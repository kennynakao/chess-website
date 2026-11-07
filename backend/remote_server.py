#!/usr/bin/env python3
"""Authenticated, persistent Pi chess API. No static files or browser credentials.

Two kinds of website game share this service and its database, chosen per game on /api/new:

  * the digital game ("board": false, the default): the website player is White and
    Stockfish answers inside the same request; nothing physical is touched;
  * the board game ("board": true): two people and no Stockfish. The person at the physical
    board plays White and the website player plays Black. The website player's move runs
    through backend/board_pipeline.py: saved, gantry, camera, then it is White's turn. The
    person at the board moves a piece by hand and taps Done on the board's screen, which sends
    /api/board-move: the camera reads the move off the board and it is saved with nothing for
    the gantry to do (the piece is already there), and it is the website player's turn again.
    Takebacks are off in a board game.

Playing in person on the touchscreen (pi_game.py / pi_touch.py on the pi-only branch) is a
third, separate program that this service never touches.

How a website game ends, besides the rules of chess: the website reports a clock at zero or
a resignation on /api/end, or the board's screen presses End Game / Reset Board (/api/online
{"reset": true}), which ends the running game as abandoned. Nothing ends a game on a timer.
Every ending clears the lease and, for a board game, queues the reset that brings the pieces
home, which the camera then checks. The screen reports a board it has verified at the
starting position with /api/online {"fresh": true}, which replaces whatever game was on with
an unstarted one so the next player finds the pairing screen, not somebody's old game.
"""
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
from backend.board_camera import camera_from_env, scan_result
from backend.board_motion import driver_from_env, plan_transition, starting_position_check
from backend.board_pipeline import BoardPipeline
from backend.chess_game import ChessGame, DIFFICULTIES, PromotionRequired, StockfishPlayer

MAX_BODY = 4096
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
REQUEST_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
UCI_PATTERN = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?\Z")
# /api/board-move is the board's screen's own, on the Pi: the website's proxy does not relay it.
ROUTES = {"/api/new", "/api/move", "/api/undo", "/api/end", "/api/motion-resolve", "/api/board-confirm", "/api/board-scan",
          "/api/board-move", "/api/online"}
PROMOTIONS = ("q", "r", "b", "n")
BOARD_OFFLINE = "The board is not in Online mode. Tap Online on the board's screen first."
BOARD_STOPPED = "The physical board stopped. Check the pieces before moving again."
BOARD_STILL_MOVING = "Wait for the board to finish the current move."
BOARD_PLAYS_WHITE = "The player at the board plays White. Wait for their move."
ONLINE_PLAYER_TO_MOVE = "It is the online player's turn. Wait for their move."
NO_TAKEBACKS = "Takebacks are off in a game on the board."
GAME_OVER = "The game is over."
ENGINE_UNAVAILABLE = "Stockfish is unavailable on the Pi."
ENGINE_FAILED_DIGITAL = "Stockfish failed. Your move was not saved; try it again in a moment."
ENGINE_FAILED_BOARD = "Stockfish failed. Your move is safe; retry when the Pi reconnects."


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str, /, **extra):
        super().__init__(message)
        self.status, self.message, self.code, self.extra = status, message, code, extra


class GameService:
    """Serialize changes; commit game and deduplication record atomically."""
    def __init__(self, database: str | Path, engine_factory=StockfishPlayer,
                 clock=time.time, lease_seconds=3600, motion_driver=None, camera=None, require_online=False):
        self.lock = threading.RLock()
        self.clock, self.lease_seconds = clock, lease_seconds
        self.seen = {}  # session -> when it last asked for anything; the board's screen uses it to see who is connected
        # Website games are allowed only while the board's touch screen has Online selected: it
        # sends /api/online every second. require_online=False (tests, bench work) skips that.
        self.require_online = require_online
        self.online_until = 0.0
        self.online_was = False
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
            # "board": a board game, played out on the physical board. "boardMoves": the game
            # history the physical board was last brought to; digital games leave it alone.
            "board": False, "boardMoves": [],
            # "ended": how the game ended when the rules of chess did not end it (see _ended), else None.
            "ended": None,
        }
        # Tells an open browser tab this is a different database, so old revision numbers no longer apply.
        self.record.setdefault("serverId", str(uuid.uuid4()))
        # Games saved before the digital game existed all ran through the board.
        self.record.setdefault("board", True)
        self.record.setdefault("boardMoves", list(self.record["uciMoves"]) if self.record["board"] else [])
        self.record.setdefault("ended", None)
        self.game = self._restore(self.record)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(self.record),))
        self._connect_engine()
        # The board game's queue and worker. Its reply stage (reply_owed / play_reply) calls back here;
        # no game owes it a reply any more, so that stage never comes (see _reply_owed).
        self.board = BoardPipeline(self.db, self.lock, motion_driver, camera,
                                   reply_owed=self._reply_owed, play_reply=self._play_reply)
        self.board.start()

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

    # ------------------------------------------------------------------ Stockfish

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
            self.engine_error = ENGINE_UNAVAILABLE
            logging.exception("ENGINE unavailable")

    def _drop_engine(self, error):
        """Forget a Stockfish that just failed; status() tries a fresh one after a pause."""
        try:
            if self.player is not None:
                self.player.close()
        except Exception:
            pass
        self.player = None
        self.last_engine_attempt = self.clock()
        self.engine_error = error

    def _require_engine(self):
        self._connect_engine()
        if self.player is None:
            raise ApiError(503, self.engine_error, "engine_unavailable")

    def _digital_reply(self, board, level):
        """The digital game: Stockfish's move now, inside the request. Nothing is committed if it fails."""
        try:
            return self.player.choose_move(board, level)
        except Exception:
            logging.exception("ENGINE failed in a digital game")
            self._drop_engine(ENGINE_FAILED_DIGITAL)
            raise ApiError(503, ENGINE_FAILED_DIGITAL, "engine_failed")

    # ------------------------------------------------------------------ snapshots

    CLIENT_SECONDS = 20  # a browser polls every 5 s, so this is "still here"

    def _note(self, session):
        now = self.clock()
        self.seen[session] = now
        if len(self.seen) > 64:
            for key in [k for k, t in self.seen.items() if now - t > self.CLIENT_SECONDS]:
                del self.seen[key]

    def _others(self, session):
        """How many other sessions have talked to the Pi in the last CLIENT_SECONDS."""
        now = self.clock()
        return sum(1 for k, t in self.seen.items() if k != session and now - t <= self.CLIENT_SECONDS)

    ONLINE_SECONDS = 10  # the screen's heartbeat comes every second; a dead screen ends Online mode

    def online_active(self):
        return not self.require_online or self.clock() < self.online_until

    def set_online(self, session, active, fresh=False, reset=False):
        """The board's screen says whether Online is selected. Returns the state the screen shows.

        With reset, someone pressed End Game / Reset Board on the screen: the game ends and the
        pieces go home (see _reset_from_screen). With fresh, the screen's camera has just seen the
        full starting position, so the old game is replaced by an unstarted one (see _fresh_board).
        Reset wins when both are sent, since fresh would claim a board the reset has not yet put back.
        """
        with self.lock:
            self._note(session)
            self.online_until = self.clock() + self.ONLINE_SECONDS if active else 0.0
            if active != self.online_was:
                logging.info("ONLINE mode %s (the board's screen)", "on" if active else "off")
                self.online_was = active
            if reset:
                self._reset_from_screen()
            elif fresh:
                self._fresh_board()
            return self._snapshot(session)

    @staticmethod
    def _over(record, game):
        """A game is over when the rules say so, or when something outside them ended it."""
        return game.board.is_game_over(claim_draw=True) or record["ended"] is not None

    @staticmethod
    def _ended(reason, loser=None, text=None):
        """The record of a game ended outside the rules of chess: a clock, a resignation, or the board's screen.

        "text" is what the website shows for it; an abandonment has no winner and no result.
        """
        winner = None if loser is None else ("b" if loser == "w" else "w")
        if text is None:
            if winner is None:
                text = "Game abandoned."
            else:
                text = f"{'White' if winner == 'w' else 'Black'} wins {'on time' if reason == 'timeout' else 'by resignation'}."
        return {"reason": reason, "winner": winner,
                "result": None if winner is None else ("1-0" if winner == "w" else "0-1"), "text": text}

    def _owned(self, session, record=None, game=None):
        record, game = record or self.record, game or self.game
        return (record["owner"] is not None and record["owner"] != session
                and record["leaseExpiresAt"] > self.clock()
                and not self._over(record, game))

    def _reply_owed(self, record=None, game=None):
        """Whether the pipeline owes a reply after the move on the board. Never, now: Stockfish plays no board game.

        In a board game White is the person at the board, whose move comes from the board's screen
        (/api/board-move), and Black is the website player. The digital game's Stockfish answers
        inside the request. The hook stays so the pipeline keeps its shape.
        """
        return False

    def _close_game(self, record):
        """The game in `record` has just ended: nobody holds it any more, and a board game puts its pieces back.

        Returns the jobs this owes the board, to queue in the caller's transaction. The camera
        stage of the reset checks the whole starting position. `boardMoves` becomes [] now, so the
        next /api/new finds an untouched board and plans nothing. The finished game itself stays
        in the record for the website to show until the next game or a fresh board.
        """
        record["owner"], record["leaseExpiresAt"] = None, 0
        if not record["board"]:
            return []
        jobs = plan_transition(record["boardMoves"], [], new_game=True)
        record["boardMoves"] = []
        return jobs

    def _fresh_board(self):
        """Under the lock. The screen's camera saw the starting position: start over from an untouched board.

        Whatever game was on, finished or not, is replaced by an unstarted one with no owner,
        so the website shows the pairing screen and anyone can start. Nothing is queued (the
        pieces are already home) and anything queued or stopped is forgotten: the board has
        been checked.
        """
        record = copy.deepcopy(self.record)
        record.update(gameId=str(uuid.uuid4()), revision=record["revision"] + 1, started=False, uciMoves=[],
                      owner=None, leaseExpiresAt=0, board=True, boardMoves=[], ended=None)
        game = ChessGame()
        game.level = record["level"]
        with self.db:
            self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(record),))
            released = self.board.drop_queued(failed=True)
        self.record, self.game = record, game
        if released:
            self.board.abandoned_job = released
        self.board.ready.notify()
        logging.info("FRESH board from the screen: game=%s revision=%s", record["gameId"], record["revision"])

    def _reset_from_screen(self):
        """Under the lock. End Game / Reset Board was pressed on the screen: the game ends and the pieces go home.

        A running game ends as abandoned, in the screen's words; a finished one just loses its
        lease. The reset is the only job left: steps nobody has done yet and a stopped board are
        forgotten, because a person is at the board. When nothing has moved since the last
        reset but a board game was played, a job with nothing to carry still runs so the camera
        checks the starting position.
        """
        record = copy.deepcopy(self.record)
        record["revision"] += 1
        if self.game.started and not self._over(record, self.game):
            record["ended"] = self._ended("abandoned", text="Game ended from the board.")
        record["owner"], record["leaseExpiresAt"] = None, 0
        jobs = []
        if record["boardMoves"] or (self.game.started and record["board"]):
            jobs = plan_transition(record["boardMoves"], [], new_game=True) or [starting_position_check()]
            record["boardMoves"] = []
        with self.db:
            self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(record),))
            released = self.board.drop_queued(failed=True)
            for job in jobs:
                self.board.queue(job)
        self.record = record
        if released:
            self.board.abandoned_job = released
        self.board.ready.notify()
        logging.info("RESET from the screen: game=%s revision=%s %s", record["gameId"], record["revision"],
                     "the board resets" if jobs else "nothing to put back")

    def _motion_status(self, record=None, game=None):
        return self.board.status(reply_owed=self._reply_owed(record, game))

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
        ended = record["ended"]
        if outcome is None and ended is not None:
            structured_outcome = {"result": ended["result"], "winner": ended["winner"], "reason": ended["reason"]}
        result = game.result_text() if outcome else (ended["text"] if ended else None)
        if outcome and record["board"] and outcome.winner is not None:
            # result_text() speaks to the digital game's player ("You win", "Stockfish wins"); a board game is two people.
            result = f"{'White' if outcome.winner else 'Black'} wins by checkmate."
        return {
            "ok": True, "device": "chessboard", "engine": self.player is not None,
            "engineError": self.engine_error, "started": game.started, "level": game.level,
            "levels": [{"id": key, "label": d.label, "description": d.description}
                       for key, d in DIFFICULTIES.items()],
            "fen": board.fen(), "uciMoves": [m.uci() for m in board.move_stack],
            "sanMoves": list(game.san_moves), "revision": record["revision"],
            "gameId": record["gameId"], "serverId": record["serverId"], "turn": "w" if board.turn else "b",
            "check": board.is_check(), "gameOver": outcome is not None or ended is not None, "outcome": structured_outcome,
            "result": result, "ended": ended,
            "lastMove": game.last_move.uci() if game.last_move else None,
            "canControl": not self._owned(session, record, game),
            "occupied": self._owned(session, record, game),
            "leaseExpiresAt": record["leaseExpiresAt"],
            "board": record["board"],
            # The website player's colour, and the colour of the person at the physical board (None: nobody is).
            "playerColor": "b" if record["board"] else "w",
            "boardColor": "w" if record["board"] else None,
            "clients": self._others(session),
            "online": self.online_active(),
            "motion": self._motion_status(record, game),
        }

    # ------------------------------------------------------------------ the pipeline's reply stage

    def _play_reply(self):
        """The pipeline's reply stage: Stockfish's move, saved and sent to the gantry. False when closing.

        Unused while _reply_owed is False (no board game has Stockfish in it); kept as the pipeline's hook.
        """
        with self.board.ready:
            self._connect_engine()
            player = self.player
            board, level, revision = self.game.board.copy(stack=True), self.game.level, self.record["revision"]
            if player is None:
                self.board.fault("stockfish", self.engine_error)
                return True
        logging.info("PIPELINE stockfish: thinking at level=%s", level)
        try:
            move = player.choose_move(board, level)
        except Exception:
            logging.exception("PIPELINE stockfish failed")
            with self.board.ready:
                if self.board.closing:
                    return False
                self._drop_engine(ENGINE_FAILED_BOARD)
                self.board.fault("stockfish", self.engine_error)
            return True
        with self.board.ready:
            self.board.stage = None
            if self.board.closing:
                return False
            if self.record["revision"] != revision:
                return True  # A new game started while it was thinking.
            candidate = copy.deepcopy(self.game)
            record = copy.deepcopy(self.record)
            san = candidate.play_computer(move)
            record.update(revision=record["revision"] + 1, uciMoves=[m.uci() for m in candidate.board.move_stack])
            jobs = plan_transition(self.record["boardMoves"], record["uciMoves"])
            record["boardMoves"] = list(record["uciMoves"])
            if self._over(record, candidate):
                # Stockfish ended the game: its move still finishes on the board, then the pieces go home.
                jobs += self._close_game(record)
            with self.db:
                self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(record),))
                for job in jobs:
                    self.board.queue(job)
            self.record, self.game = record, candidate
            logging.info("PIPELINE stockfish: played %s (%s)%s", move.uci(), san, "; game over, the board resets" if len(jobs) > 1 else "")
        return True

    # ------------------------------------------------------------------ board-only requests

    def resolve_motion(self, session):
        """Someone checked the board and it matches the screen, so the queue restarts from here."""
        with self.board.ready:
            if self._owned(session):
                raise ApiError(409, "Another player is controlling the board.", "board_busy", state=self._snapshot(session))
            self.board.resolve()
            return self._snapshot(session)

    def confirm_board(self, session, override=False, job_id=None):
        """The person at the board says they are done. With override, the camera is not consulted."""
        with self.board.ready:
            if self._owned(session):
                raise ApiError(409, "Another player is controlling the board.", "board_busy", state=self._snapshot(session))
            self.board.confirm(job_id, override)
            return self._snapshot(session)

    def scan_board(self, session):
        """Photograph the board and report which legal moves it could be showing. Commits nothing."""
        if self.board.camera is None:
            raise ApiError(409, "No camera is connected to the board.", "no_camera")
        with self.lock:
            if self._owned(session):
                raise ApiError(409, "Another player is controlling the board.", "board_busy", state=self._snapshot(session))
            if self.game.started and not self.record["board"]:
                raise ApiError(409, "Reading the board is only for games played on the board.", "not_board_game")
            if self._motion_status()["state"] != "idle":
                raise ApiError(409, "Finish the moves on the board first.", "board_moving", state=self._snapshot(session))
            if not self.game.started or self.game.board.turn != chess.WHITE or self._over(self.record, self.game):
                raise ApiError(409, "It is not your turn.", "not_your_turn")
            board, revision = self.game.board.copy(), self.record["revision"]
        try:
            observation = self.board.observe()
        except Exception as exc:
            logging.exception("CAMERA failed during a scan")
            raise ApiError(503, "The camera could not take a photo.", "camera_failed") from exc
        with self.lock:
            if self.record["revision"] != revision:
                raise ApiError(409, "The game changed. Reload its current state.", "stale_state", state=self._snapshot(session))
        return {"ok": True, "revision": revision, **scan_result(board, observation)}

    # ------------------------------------------------------------------ the board's screen: White's move

    def board_move(self, session, body):
        """The board's screen: the person at the board has made White's move by hand and tapped Done.

        The camera reads the move off the board, and it is committed like a website move, through
        the same checks and deduplication (a requestId is made up when the screen sends none), with
        two differences: nothing is planned for the gantry, because the piece is already there, and
        the website player keeps the lease. A photo that does not show exactly one legal move
        commits nothing and says why (409 board_move_unclear, with what the camera saw).

        The camera sees which squares are full, not which piece is on them, so a piece that could
        capture on more than one square looks the same whichever it took. The screen can then send
        the one the person made as "move" (UCI, one of the candidates it was given).
        """
        request_id = body.get("requestId")
        if request_id is None:
            request_id = "board-" + uuid.uuid4().hex
        elif not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id):
            raise ApiError(400, "A valid requestId is required.", "invalid_request")
        promotion, chosen = body.get("promotion"), body.get("move")
        if promotion is not None and promotion not in PROMOTIONS:
            raise ApiError(400, "promotion must be q, r, b or n.", "invalid_move")
        if chosen is not None and not (isinstance(chosen, str) and UCI_PATTERN.fullmatch(chosen)):
            raise ApiError(400, "move must be a move in UCI, such as e2e4.", "invalid_move")
        fingerprint = self._fingerprint(session, "/api/board-move", body)
        with self.lock:
            cached = self._replay(request_id, fingerprint)
            if cached is not None:
                return cached
            self._require_board_turn(session)
            board, revision, game_id = self.game.board.copy(), self.record["revision"], self.record["gameId"]
        try:
            observation = self.board.observe()  # Not under the lock: a photo takes seconds.
        except Exception as exc:
            logging.exception("CAMERA failed reading the move at the board")
            raise ApiError(503, "The camera could not take a photo.", "camera_failed") from exc
        with self.lock:
            # The same request, retried while this one was looking, may have committed already.
            cached = self._replay(request_id, fingerprint)
            if cached is not None:
                return cached
            if self.record["revision"] != revision:
                raise ApiError(409, "The game changed while the camera was looking. Tap Done again.", "stale_state",
                               state=self._snapshot(session))
            move = self._read_move(scan_result(board, observation), promotion, chosen)
            logging.info("BOARD the camera read %s (%s) at the board", move["uci"], move["san"])
            command = {"expectedGameId": game_id, "expectedRevision": revision,
                       "from": move["from"], "to": move["to"], "promotion": move["promotion"]}
            return self._commit("/api/board-move", command, session, request_id, fingerprint)

    def _require_board_turn(self, session):
        """Under the lock. /api/board-move needs White to move in a running board game, an idle board and a camera."""
        def refuse(message, code):
            raise ApiError(409, message, code, state=self._snapshot(session))
        if not self.online_active():
            refuse(BOARD_OFFLINE, "board_offline")
        if not self.record["board"]:
            refuse("The game on the website is played on screen, not on the board.", "not_board_game")
        if not self.game.started:
            refuse("No game on the board has started.", "not_board_game")
        if self._over(self.record, self.game):
            refuse(GAME_OVER, "game_over")
        motion = self._motion_status()["state"]
        if motion == "fault":
            refuse(BOARD_STOPPED, "board_fault")
        if motion != "idle":
            refuse(BOARD_STILL_MOVING, "board_moving")
        if self.game.board.turn != chess.WHITE:
            refuse(ONLINE_PLAYER_TO_MOVE, "not_board_turn")
        if self.board.camera is None:
            refuse("No camera is connected to the board.", "no_camera")

    @staticmethod
    def _read_move(scan, promotion, chosen=None):
        """The one legal move a photo of the board shows, else 409 board_move_unclear with the scan.

        The camera sees which squares are full, not which piece is on them, so a pawn reaching the
        last rank matches all four promotions: then the screen's promotion decides, or a queen.
        A move the screen chose (`chosen`) must be one of those the photo fits.
        """
        candidates, status = scan["candidates"], scan["status"]
        fits = candidates if chosen is None else [c for c in candidates if c["uci"] in (chosen, chosen + (promotion or "q"))]
        if len(fits) == 1:
            return fits[0]
        if fits and len({(c["from"], c["to"]) for c in fits}) == 1:
            pick = next((c for c in fits if c["promotion"] == (promotion or "q")), None)
            if pick is not None:
                return pick
        if chosen is not None and candidates:
            message = (f"The camera does not see {chosen} on the board; it fits {', '.join(c['san'] for c in candidates)}. "
                       "Check the pieces, then tap Done again.")
        elif status == "unchanged":
            message = "No move seen on the board yet. Make your move, then tap Done."
        elif status == "uncertain":
            message = f"The camera could not read {', '.join(scan['uncertain'])}. Check those squares, then tap Done again."
        elif status == "ambiguous":
            message = f"More than one move fits what the camera sees: {', '.join(c['san'] for c in candidates)}."
        else:
            seen = ([f"no piece on {', '.join(scan['missing'])}"] if scan["missing"] else []) + \
                   ([f"an extra piece on {', '.join(scan['extra'])}"] if scan["extra"] else [])
            message = ("The board does not match a legal move" + (f": the camera sees {' and '.join(seen)}" if seen else "") +
                       ". Check the pieces, then tap Done again.")
        raise ApiError(409, message, "board_move_unclear",
                       **{key: scan[key] for key in ("status", "candidates", "missing", "extra", "uncertain")})

    # ------------------------------------------------------------------ the game

    def status(self, session):
        with self.lock:
            self._note(session)
            self._connect_engine(throttled=True)
            return self._snapshot(session)

    def mutate(self, route, body, session):
        if not SESSION_PATTERN.fullmatch(session or ""):
            raise ApiError(400, "Missing or invalid player session.", "invalid_session")
        if route not in ROUTES:
            raise ApiError(404, "Unknown API route.", "not_found")
        with self.lock:
            self._note(session)
        if route == "/api/online":
            return self.set_online(session, body.get("active") is True, fresh=body.get("fresh") is True, reset=body.get("reset") is True)
        if route == "/api/motion-resolve":
            return self.resolve_motion(session)
        if route == "/api/board-confirm":
            job_id = body.get("jobId")
            return self.confirm_board(session, body.get("override") is True, job_id if type(job_id) is int else None)
        if route == "/api/board-scan":
            return self.scan_board(session)
        if route == "/api/board-move":
            return self.board_move(session, body)
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not REQUEST_PATTERN.fullmatch(request_id):
            raise ApiError(400, "A valid requestId is required.", "invalid_request")
        fingerprint = self._fingerprint(session, route, body)
        with self.lock:
            cached = self._replay(request_id, fingerprint)
            if cached is not None:
                return cached
            return self._commit(route, body, session, request_id, fingerprint)

    @staticmethod
    def _fingerprint(session, route, body):
        return hashlib.sha256(json.dumps([session, route, body], sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _replay(self, request_id, fingerprint):
        """Under the lock. The saved answer when this request was committed before, else None."""
        cached = self.db.execute("SELECT fingerprint,response FROM requests WHERE id=?", (request_id,)).fetchone()
        if not cached:
            return None
        if not hmac.compare_digest(cached[0], fingerprint):
            raise ApiError(409, "requestId was already used for a different command.", "request_collision")
        logging.info("REPLAY request=%s", request_id)
        return json.loads(cached[1])

    def _commit(self, route, body, session, request_id, fingerprint):
        """Under the lock. Check one command against the game, apply it, and save it with its answer for replays.

        /api/board-move comes here from board_move() as White's move in a board game, already read
        off the board by the camera. It is the /api/move branch with three differences: it is the
        board's screen asking, so the lease is not checked and stays with the website player; it
        plays White, not the website player's Black; and nothing is planned for the gantry.
        """
        by_board = route == "/api/board-move"
        revision = body.get("expectedRevision")
        if type(revision) is not int or not isinstance(body.get("expectedGameId"), str):
            raise ApiError(400, "expectedGameId and integer expectedRevision are required.", "invalid_version")
        if revision != self.record["revision"] or body["expectedGameId"] != self.record["gameId"]:
            raise ApiError(409, "The game changed. Reload its current state.", "stale_state", state=self._snapshot(session))
        if not by_board and self._owned(session):
            raise ApiError(409, "Another player is controlling the board.", "board_busy", state=self._snapshot(session))
        if not self.online_active():
            raise ApiError(409, BOARD_OFFLINE, "board_offline", state=self._snapshot(session))
        # A new game chooses: on the physical board, or on screen only. Later requests follow the game.
        board_game = body.get("board") is True if route == "/api/new" else self.record["board"]
        if by_board and not board_game:
            raise ApiError(409, "The game on the website is played on screen, not on the board.", "not_board_game")
        if route == "/api/undo" and board_game:
            # Two people play a board game, and a takeback would take the other player's move off the board too.
            raise ApiError(409, NO_TAKEBACKS, "no_takebacks", state=self._snapshot(session))
        starting_over = route == "/api/new" and board_game and self.board.driver.needs_person
        if board_game:
            motion = self._motion_status()
            if motion["state"] == "fault":
                raise ApiError(409, BOARD_STOPPED, "board_fault", state=self._snapshot(session))
            # One move at a time, all the way through the pipeline. A person can still abandon theirs,
            # and a clock or a resignation can end the game while the board is still moving: the
            # move in progress finishes and the reset queues behind it.
            if motion["state"] != "idle" and not starting_over and route != "/api/end":
                raise ApiError(409, BOARD_STILL_MOVING, "board_moving", state=self._snapshot(session))
        candidate = copy.deepcopy(self.game)
        record = copy.deepcopy(self.record)
        extra = {}
        if route == "/api/new":
            level = body.get("level", "beginner")
            if not isinstance(level, str) or level not in DIFFICULTIES:
                raise ApiError(400, "Choose a valid difficulty.", "invalid_level")
            if not board_game:
                # Only the digital game has Stockfish in it. A board game keeps the level but never uses it.
                self._require_engine()
            candidate.start(level)
            record["gameId"] = str(uuid.uuid4())
            record["board"] = board_game
            record["ended"] = None
        elif route == "/api/end":
            # The website saw a clock reach zero or the player resign. The rules of chess have not
            # spoken, so the record says how it ended and the snapshot shows it until the next game.
            reason, loser = body.get("reason"), body.get("loser")
            if reason not in ("timeout", "resignation") or loser not in ("w", "b"):
                raise ApiError(400, "reason must be timeout or resignation, and loser w or b.", "invalid_end")
            if not candidate.started:
                raise ApiError(400, "No game has started.", "not_started")
            if self._over(record, candidate):
                raise ApiError(400, GAME_OVER, "game_over")
            record["ended"] = self._ended(reason, loser)
        elif route in ("/api/move", "/api/board-move"):
            if self._over(record, candidate):
                raise ApiError(400, GAME_OVER, "game_over")
            # Whose move this is: the digital game's website player is White. In a board game the person
            # at the board is White, through the board's screen, and the website player is Black.
            color = chess.WHITE if by_board or not board_game else chess.BLACK
            if board_game and candidate.started and candidate.board.turn != color:
                if by_board:
                    raise ApiError(409, ONLINE_PLAYER_TO_MOVE, "not_board_turn", state=self._snapshot(session))
                raise ApiError(409, BOARD_PLAYS_WHITE, "not_your_turn", state=self._snapshot(session))
            source, target, promotion = body.get("from"), body.get("to"), body.get("promotion")
            if not isinstance(source, str) or not isinstance(target, str) or (promotion is not None and not isinstance(promotion, str)):
                raise ApiError(400, "Move squares and promotion must be strings.", "invalid_move")
            try:
                san = candidate.play_human(source, target, promotion, color)
            except PromotionRequired:
                raise ApiError(409, "Choose a promotion piece.", "promotion_required", promotionRequired=True)
            except ValueError as exc:
                raise ApiError(400, str(exc), "invalid_move") from exc
            played = {"uci": candidate.last_move.uci(), "san": san}
            if by_board:
                extra["boardMove"] = played
            else:
                extra["human"], extra["computer"] = played, None
            if not board_game and not candidate.board.is_game_over(claim_draw=True):
                # The digital game: Stockfish answers now, in this request. Nothing moves.
                self._require_engine()
                move = self._digital_reply(candidate.board.copy(stack=True), candidate.level)
                extra["computer"] = {"uci": move.uci(), "san": candidate.play_computer(move)}
            # In a board game the other player answers: White on the board, or Black on the website.
        else:
            if not candidate.started or not candidate.board.move_stack:
                raise ApiError(400, "Nothing to take back.", "nothing_to_undo")
            if self._over(record, candidate):
                raise ApiError(400, GAME_OVER, "game_over")
            count = min(2 if candidate.board.turn == chess.WHITE else 1, len(candidate.board.move_stack))
            for _ in range(count):
                candidate.board.pop()
                candidate.san_moves.pop()
            candidate.last_move = candidate.board.peek() if candidate.board.move_stack else None
            extra["undone"] = count
        record.update(revision=record["revision"] + 1, started=candidate.started,
                      level=candidate.level, uciMoves=[m.uci() for m in candidate.board.move_stack])
        if not by_board:
            record.update(owner=session, leaseExpiresAt=self.clock() + self.lease_seconds)
        elif record["owner"] is not None:
            # The screen never takes the game: the website player keeps it, for a full lease from this move.
            record["leaseExpiresAt"] = self.clock() + self.lease_seconds
        jobs = []
        if board_game:
            # From wherever the physical pieces last were (digital games never move them) to this game.
            # A move read off the board is already there, so only what came before it is planned: normally nothing.
            target = self.record["uciMoves"] if by_board else record["uciMoves"]
            jobs = plan_transition(self.record["boardMoves"], target, new_game=route == "/api/new")
            # A person sets up the board once, whatever was still queued, and as in pi_game.py
            # it is checked before the first move.
            if starting_over:
                jobs = [starting_position_check()]
            record["boardMoves"] = list(record["uciMoves"])
        if route != "/api/new" and self._over(record, candidate):
            # Checkmate, a draw, a clock or a resignation: the lease ends with the game, and a board game
            # puts its pieces back behind whatever this request still owes the board.
            jobs += self._close_game(record)
        # The game and the signals it owes the board commit together, or not at all.
        with self.db:
            self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(record),))
            released = self.board.drop_queued() if starting_over else None
            for job in jobs:
                self.board.queue(job)
            response = {**self._snapshot(session, record, candidate), **extra, "requestId": request_id}
            self.db.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, fingerprint, json.dumps(response)))
        self.record, self.game = record, candidate
        if released:
            # Release the worker, which is still waiting on a step that no longer exists.
            self.board.abandoned_job = released
        self.board.ready.notify()
        played = extra.get("human") or extra.get("boardMove") or {}
        logging.info("COMMIT route=%s request=%s game=%s revision=%s %s move=%s fen=%s%s",
                     route, request_id, record["gameId"], record["revision"], "board" if board_game else "digital",
                     played.get("uci", "-"), candidate.board.fen(),
                     f" ended={record['ended']['reason']}" if record["ended"] else "")
        return response

    def close(self):
        self.board.close()
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
    always_online = os.environ.get("CHESS_ONLINE_ALWAYS", "").strip() == "1"
    service = GameService(path, motion_driver=driver_from_env(), camera=camera_from_env(), require_online=not always_online)
    server = create_server(service, token, port=args.port)
    logging.info("Pi API ready at http://127.0.0.1:%s; device=chessboard engine=%s gantry=%s camera=%s online=%s", args.port,
                 service.player is not None, service.board.driver.name, service.board.camera is not None,
                 "always" if always_online else "when the screen selects Online")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
