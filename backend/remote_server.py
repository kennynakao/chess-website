#!/usr/bin/env python3
"""Authenticated, persistent Pi chess API. No static files or browser credentials."""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import chess
from backend.chess_game import ChessGame, DIFFICULTIES, PromotionRequired, StockfishPlayer

MAX_BODY = 4096
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
REQUEST_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ROUTES = {"/api/new", "/api/move", "/api/undo"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str, **extra):
        super().__init__(message)
        self.status, self.message, self.code, self.extra = status, message, code, extra


class GameService:
    """Serialize changes; commit game and deduplication record atomically."""
    def __init__(self, database: str | Path, engine_factory=StockfishPlayer,
                 clock=time.time, lease_seconds=3600):
        self.lock = threading.RLock()
        self.clock, self.lease_seconds = clock, lease_seconds
        self.engine_factory = engine_factory
        self.player = None
        self.engine_error = None
        self.last_engine_attempt = float("-inf")
        path = Path(database).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)")
        row = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()
        self.record = json.loads(row[0]) if row else {
            "gameId": str(uuid.uuid4()), "revision": 0, "started": False,
            "level": "beginner", "uciMoves": [], "owner": None, "leaseExpiresAt": 0,
        }
        self.game = self._restore(self.record)
        if not row:
            with self.db:
                self.db.execute("INSERT INTO state VALUES(1,?)", (json.dumps(self.record),))
        self._connect_engine()

    @staticmethod
    def _restore(record):
        game = ChessGame()
        game.started, game.level = record["started"], record["level"]
        for uci in record["uciMoves"]:
            move = chess.Move.from_uci(uci)
            if move not in game.board.legal_moves:
                raise RuntimeError("Saved game contains an invalid move")
            game._push(move)
        return game

    def _connect_engine(self, throttled=False):
        if self.player is not None:
            return
        if throttled and self.clock() - self.last_engine_attempt < 15:
            return
        self.last_engine_attempt = self.clock()
        try:
            self.player = self.engine_factory()
            self.engine_error = None
        except Exception:
            self.engine_error = "Stockfish is unavailable on the Pi."
            logging.exception("ENGINE unavailable")

    def _owned(self, session, record=None, game=None):
        record, game = record or self.record, game or self.game
        return (record["owner"] is not None and record["owner"] != session
                and record["leaseExpiresAt"] > self.clock()
                and not game.board.is_game_over(claim_draw=True))

    def _snapshot(self, session, record=None, game=None):
        record, game = record or self.record, game or self.game
        board = game.board
        outcome = board.outcome(claim_draw=True)
        structured_outcome = None
        if outcome:
            reasons = {
                chess.Termination.CHECKMATE: "checkmate",
                chess.Termination.STALEMATE: "stalemate",
                chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material",
                chess.Termination.THREEFOLD_REPETITION: "threefold repetition",
                chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition",
                chess.Termination.FIFTY_MOVES: "50-move rule",
                chess.Termination.SEVENTYFIVE_MOVES: "75-move rule",
            }
            structured_outcome = {
                "result": outcome.result(),
                "winner": None if outcome.winner is None else ("w" if outcome.winner else "b"),
                "reason": reasons.get(outcome.termination, "draw"),
            }
        return {
            "ok": True, "device": "chessboard", "engine": self.player is not None,
            "engineError": self.engine_error, "started": game.started, "level": game.level,
            "levels": [{"id": key, "label": d.label, "description": d.description}
                       for key, d in DIFFICULTIES.items()],
            "fen": board.fen(), "uciMoves": [m.uci() for m in board.move_stack],
            "sanMoves": list(game.san_moves), "revision": record["revision"],
            "gameId": record["gameId"], "turn": "w" if board.turn else "b",
            "check": board.is_check(), "gameOver": outcome is not None, "outcome": structured_outcome,
            "result": game.result_text(), "lastMove": game.last_move.uci() if game.last_move else None,
            "canControl": not self._owned(session, record, game),
            "occupied": self._owned(session, record, game),
            "leaseExpiresAt": record["leaseExpiresAt"],
        }

    def status(self, session):
        with self.lock:
            self._connect_engine(throttled=True)
            return self._snapshot(session)

    def mutate(self, route, body, session):
        if not SESSION_PATTERN.fullmatch(session or ""):
            raise ApiError(400, "Missing or invalid player session.", "invalid_session")
        if route not in ROUTES:
            raise ApiError(404, "Unknown API route.", "not_found")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id):
            raise ApiError(400, "A valid requestId is required.", "invalid_request")
        fingerprint = hashlib.sha256(json.dumps([session, route, body], sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        with self.lock:
            cached = self.db.execute("SELECT fingerprint,response FROM requests WHERE id=?", (request_id,)).fetchone()
            if cached:
                if not hmac.compare_digest(cached[0], fingerprint):
                    raise ApiError(409, "requestId was already used for a different command.", "request_collision")
                logging.info("REPLAY request=%s", request_id)
                return json.loads(cached[1])
            revision = body.get("expectedRevision")
            if type(revision) is not int or not isinstance(body.get("expectedGameId"), str):
                raise ApiError(400, "expectedGameId and integer expectedRevision are required.", "invalid_version")
            if revision != self.record["revision"] or body["expectedGameId"] != self.record["gameId"]:
                raise ApiError(409, "The game changed. Reload its current state.", "stale_state", state=self._snapshot(session))
            if self._owned(session):
                raise ApiError(409, "Another player is controlling the board.", "board_busy", state=self._snapshot(session))
            candidate = copy.deepcopy(self.game)
            record = copy.deepcopy(self.record)
            extra = {}
            if route == "/api/new":
                level = body.get("level", "beginner")
                if not isinstance(level, str) or level not in DIFFICULTIES:
                    raise ApiError(400, "Choose a valid difficulty.", "invalid_level")
                self._connect_engine()
                if self.player is None:
                    raise ApiError(503, self.engine_error, "engine_unavailable")
                candidate.start(level)
                record["gameId"] = str(uuid.uuid4())
            elif route == "/api/move":
                source, target, promotion = body.get("from"), body.get("to"), body.get("promotion")
                if not isinstance(source, str) or not isinstance(target, str) or (promotion is not None and not isinstance(promotion, str)):
                    raise ApiError(400, "Move squares and promotion must be strings.", "invalid_move")
                try:
                    human_san = candidate.play_human(source, target, promotion)
                except PromotionRequired:
                    raise ApiError(409, "Choose a promotion piece.", "promotion_required", promotionRequired=True)
                except ValueError as exc:
                    raise ApiError(400, str(exc), "invalid_move") from exc
                extra["human"] = {"uci": candidate.last_move.uci(), "san": human_san}
                extra["computer"] = None
                if not candidate.board.is_game_over(claim_draw=True):
                    self._connect_engine()
                    if self.player is None:
                        raise ApiError(503, self.engine_error, "engine_unavailable")
                    logging.info("STOCKFISH thinking request=%s game=%s human=%s level=%s", request_id, record["gameId"], extra["human"]["uci"], candidate.level)
                    try:
                        move = self.player.choose_move(candidate.board.copy(stack=True), candidate.level)
                        san = candidate.play_computer(move)
                    except Exception as exc:
                        logging.exception("STOCKFISH failed request=%s; no game changes committed", request_id)
                        try:
                            self.player.close()
                        except Exception:
                            pass
                        self.player = None
                        self.last_engine_attempt = self.clock()
                        self.engine_error = "Stockfish failed. Your move was not committed; retry when the Pi reconnects."
                        raise ApiError(502, self.engine_error, "engine_failed") from exc
                    extra["computer"] = {"uci": move.uci(), "san": san}
            else:
                if not candidate.started or not candidate.board.move_stack:
                    raise ApiError(400, "Nothing to take back.", "nothing_to_undo")
                count = min(2 if candidate.board.turn == chess.WHITE else 1, len(candidate.board.move_stack))
                for _ in range(count):
                    candidate.board.pop()
                    candidate.san_moves.pop()
                candidate.last_move = candidate.board.peek() if candidate.board.move_stack else None
                extra["undone"] = count
            record.update(revision=record["revision"] + 1, started=candidate.started,
                          level=candidate.level, uciMoves=[m.uci() for m in candidate.board.move_stack],
                          owner=session, leaseExpiresAt=self.clock() + self.lease_seconds)
            response = {**self._snapshot(session, record, candidate), **extra, "requestId": request_id}
            with self.db:
                self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(record),))
                self.db.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, fingerprint, json.dumps(response)))
            self.record, self.game = record, candidate
            logging.info("COMMIT route=%s request=%s game=%s revision=%s human=%s stockfish=%s fen=%s",
                         route, request_id, record["gameId"], record["revision"],
                         extra.get("human", {}).get("uci", "-"), (extra.get("computer") or {}).get("uci", "-"), candidate.board.fen())
            return response

    def close(self):
        with self.lock:
            if self.player:
                self.player.close()
            self.db.close()


def create_server(service, token, host="127.0.0.1", port=8765):
    if not token or len(token) < 32:
        raise ValueError("CHESS_RELAY_TOKEN must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChessRelay/1"

        def _send(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                logging.info("CLIENT disconnected; retry the same requestId to recover committed result")

        def _handle(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                raise ApiError(401, "Unauthorized.", "unauthorized")
            session = self.headers.get("X-Player-Session", "")
            if not SESSION_PATTERN.fullmatch(session):
                raise ApiError(400, "Missing or invalid player session.", "invalid_session")
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/api/status":
                return service.status(session)
            if self.command != "POST" or path not in ROUTES:
                raise ApiError(404, "Unknown API route.", "not_found")
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "Transfer encoding is not supported.", "invalid_body")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ApiError(400, "Content-Length is required.", "invalid_body")
            try:
                length = int(lengths[0])
            except ValueError:
                raise ApiError(400, "Invalid Content-Length.", "invalid_body")
            if length < 1 or length > MAX_BODY:
                raise ApiError(413, "Request body is too large or empty.", "invalid_body")
            if self.headers.get_content_type() != "application/json":
                raise ApiError(415, "Use application/json.", "invalid_body")
            self.connection.settimeout(10)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError, TimeoutError):
                raise ApiError(400, "Invalid JSON request.", "invalid_body")
            if not isinstance(body, dict):
                raise ApiError(400, "Request body must be an object.", "invalid_body")
            return service.mutate(path, body, session)

        def dispatch(self):
            try:
                self._send(200, self._handle())
            except ApiError as exc:
                self._send(exc.status, {**exc.extra, "ok": False, "error": exc.message, "code": exc.code})
            except Exception:
                logging.exception("API failure")
                self._send(500, {"ok": False, "error": "The Pi could not complete the request.", "code": "internal_error"})

        do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_HEAD = dispatch

        def log_message(self, fmt, *args):
            # No headers, tokens, session values, or arbitrary URL query data in logs.
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    token = os.environ.get("CHESS_RELAY_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("Set CHESS_RELAY_TOKEN to a random secret of at least 32 characters")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    path = Path(os.environ.get("CHESS_STATE_PATH", "~/.local/share/duen-chess/state.sqlite3")).expanduser().resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise SystemExit("CHESS_STATE_PATH must be outside the source repository")
    service = GameService(path)
    server = create_server(service, token, port=args.port)
    logging.info("Pi API ready at http://127.0.0.1:%s; device=chessboard engine=%s", args.port, service.player is not None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
