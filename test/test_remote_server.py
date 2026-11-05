"""Regression tests for authoritative remote game transactions and API exposure.

RemoteGameTests play the digital game (the default). The QueueTestCase classes play
board games, where every move runs through the gantry, the camera and Stockfish.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock
import chess

from backend.board_camera import BoardObservation, occupied_squares
from backend.board_motion import ManualDriver, driver_from_env
from backend import gantry
from backend.gantry import NOT_DONE, ArduinoGantry, GantryDriver
from backend.remote_server import ApiError, GameService, create_server

ALICE = "a" * 32
BOB = "b" * 32
SCREEN = "touchscreen-" + "c" * 20
TOKEN = "t" * 64


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def choose_move(self, board, level):
        self.calls += 1
        if self.fail:
            raise RuntimeError("engine disconnected")
        return next(iter(board.legal_moves))

    def close(self):
        pass


class RemoteGameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.engine = FakeEngine()
        self.now = [1000]
        self.service = GameService(self.path, lambda: self.engine, lambda: self.now[0], lease_seconds=60)
        self.seq = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def command(self, route, player=ALICE, **fields):
        state = self.service.status(player)
        self.seq += 1
        body = {"requestId": "req-" + str(self.seq), "expectedGameId": state["gameId"],
                "expectedRevision": state["revision"], **fields}
        return self.service.mutate(route, body, player), body

    def settled(self, expected="idle"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            motion = self.service.status(ALICE)["motion"]
            if motion["state"] == expected:
                return motion
            time.sleep(0.005)
        self.fail(f"motion never reached {expected}: {motion}")

    def test_duplicate_move_returns_same_reply_exactly_once(self):
        self.command("/api/new", level="beginner")
        first, body = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(first["uciMoves"][0], "e2e4")
        self.assertEqual(len(first["uciMoves"]), 2, "the digital game answers in the same request")
        self.assertEqual(first["computer"]["uci"], first["uciMoves"][1])
        self.assertFalse(first["board"])
        self.assertEqual(self.service.mutate("/api/move", body, ALICE), first)
        self.settled()
        self.assertEqual(self.service.mutate("/api/move", body, ALICE), first)
        self.assertEqual(self.engine.calls, 1)
        state = self.service.status(ALICE)
        self.assertEqual(len(state["uciMoves"]), 2)
        self.assertEqual(state["revision"], 2)
    def test_concurrent_retries_do_not_duplicate_engine_call(self):
        self.command("/api/new")
        state = self.service.status(ALICE)
        body = dict(requestId="parallel", expectedGameId=state["gameId"], expectedRevision=state["revision"], **{"from": "e2", "to": "e4"})
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.service.mutate("/api/move", body, ALICE))) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 4)
        self.assertTrue(all(x == results[0] for x in results))
        self.settled()
        self.assertEqual(self.engine.calls, 1)
    def test_colliding_request_id_rejected_for_changed_body_or_owner(self):
        first, body = self.command("/api/new")
        for changed, owner in [({**body, "level": "advanced"}, ALICE), (body, BOB)]:
            with self.assertRaises(ApiError) as caught:
                self.service.mutate("/api/new", changed, owner)
            self.assertEqual(caught.exception.code, "request_collision")
        self.assertEqual(self.service.status(ALICE)["gameId"], first["gameId"])

    def test_stale_revision_and_game_guard_all_mutations(self):
        old = self.service.status(ALICE)
        self.command("/api/new")
        for route in ["/api/new", "/api/move", "/api/undo", "/api/end"]:
            with self.assertRaises(ApiError) as caught:
                self.service.mutate(route, dict(requestId="stale"+route.rsplit("/",1)[1], expectedGameId=old["gameId"], expectedRevision=old["revision"]), ALICE)
            self.assertEqual(caught.exception.code, "stale_state")
            self.assertEqual(caught.exception.extra["state"]["revision"], 1)

    def test_other_owner_blocked_until_lease_expires(self):
        self.command("/api/new")
        self.assertFalse(self.service.status(BOB)["canControl"])
        with self.assertRaises(ApiError) as caught:
            self.command("/api/new", BOB)
        self.assertEqual(caught.exception.code, "board_busy")
        self.now[0] += 61
        self.assertTrue(self.service.status(BOB)["canControl"])
        self.command("/api/new", BOB)
        self.assertFalse(self.service.status(ALICE)["canControl"])

    def test_illegal_moves_do_not_commit(self):
        self.command("/api/new")
        before = copy.deepcopy(self.service.record)
        with self.assertRaises(ApiError):
            self.command("/api/move", **{"from": "e2", "to": "e5"})
        self.assertEqual(self.service.record, before)

    def test_a_failing_engine_refuses_the_digital_move_and_recovers(self):
        self.command("/api/new")
        before = copy.deepcopy(self.service.record)
        self.engine.fail = True
        with self.assertLogs(level="ERROR"), self.assertRaises(ApiError) as caught:
            self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(caught.exception.code, "engine_failed")
        self.assertEqual(self.service.record, before, "nothing is committed")
        self.assertFalse(self.service.status(ALICE)["engine"])
        self.engine.fail = False
        self.now[0] += 15
        self.assertTrue(self.service.status(ALICE)["engine"])
        response, _ = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(len(response["uciMoves"]), 2)

    def test_a_dead_engine_refuses_the_move_before_anything_is_committed(self):
        self.command("/api/new")
        before = copy.deepcopy(self.service.record)
        self.service.player, self.service.engine_factory = None, mock.Mock(side_effect=OSError("gone"))
        with self.assertLogs(level="ERROR"), self.assertRaises(ApiError) as caught:
            self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(caught.exception.code, "engine_unavailable")
        self.assertEqual(self.service.record, before)
    def test_undo_is_atomic_and_deduplicated(self):
        self.command("/api/new")
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.settled()
        response, body = self.command("/api/undo")
        self.assertEqual(response["uciMoves"], [])
        self.assertEqual(response["undone"], 2)
        self.assertEqual(self.service.mutate("/api/undo", body, ALICE), response)
    def test_state_and_deduplication_survive_restart(self):
        self.command("/api/new", level="advanced")
        response, body = self.command("/api/move", **{"from": "d2", "to": "d4"})
        self.settled()
        before = self.service.status(ALICE)
        self.service.close()
        self.service = GameService(self.path, lambda: self.engine, lambda: self.now[0], lease_seconds=60)
        actual = self.service.status(ALICE)
        for key in ["fen", "uciMoves", "sanMoves", "revision", "gameId", "level", "leaseExpiresAt"]:
            self.assertEqual(actual[key], before[key])
        self.assertEqual(self.service.mutate("/api/move", body, ALICE), response)
        self.assertEqual(self.engine.calls, 1)

    def test_a_restart_before_stockfish_replied_still_plays_the_reply(self):
        self.command("/api/new")
        self.service.close()
        db = sqlite3.connect(self.path)
        record = json.loads(db.execute("SELECT value FROM state").fetchone()[0])
        record.update(board=True, uciMoves=["e2e4"], boardMoves=["e2e4"])
        with db:
            db.execute("UPDATE state SET value=?", (json.dumps(record),))
        db.close()
        self.service = GameService(self.path, lambda: self.engine, lambda: self.now[0], lease_seconds=60)
        self.settled()
        self.assertEqual(len(self.service.status(ALICE)["uciMoves"]), 2)
        self.assertEqual(self.engine.calls, 1)
    def test_status_recovers_engine_after_startup_failure_with_throttling(self):
        self.service.close()
        attempts = []
        def factory():
            attempts.append(self.now[0])
            if len(attempts) == 1:
                raise OSError("engine temporarily missing")
            return self.engine
        with self.assertLogs(level="ERROR"):
            self.service = GameService(self.path, factory, lambda: self.now[0])
        for _ in range(4):
            self.assertFalse(self.service.status(ALICE)["engine"])
        self.assertEqual(len(attempts), 1)
        self.now[0] += 15
        recovered = self.service.status(ALICE)
        self.assertTrue(recovered["engine"])
        self.assertIsNone(recovered["engineError"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(recovered["revision"], 0)

    def test_status_counts_the_other_sessions_seen_recently(self):
        self.assertEqual(self.service.status(ALICE)["clients"], 0, "nobody else yet")
        self.service.status(BOB)
        self.assertEqual(self.service.status(ALICE)["clients"], 1, "Bob is connected")
        self.assertEqual(self.service.status(BOB)["clients"], 1, "and from Bob's side, Alice is")
        self.now[0] += 21
        self.assertEqual(self.service.status(ALICE)["clients"], 0, "Bob went quiet")
        self.command("/api/new", BOB)
        self.assertEqual(self.service.status(ALICE)["clients"], 1, "a move counts too")

    def test_claimable_repetition_has_authoritative_structured_outcome(self):
        self.service.game.start("beginner")
        for uci in ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"]:
            self.service.game._push(chess.Move.from_uci(uci))
        self.assertFalse(self.service.game.board.is_repetition(3))
        self.assertTrue(self.service.game.board.can_claim_threefold_repetition())
        state = self.service.status(ALICE)
        self.assertTrue(state["gameOver"])
        self.assertEqual(state["outcome"], {
            "result": "1/2-1/2", "winner": None, "reason": "threefold repetition",
        })

    def test_claimable_fifty_moves_has_authoritative_structured_outcome(self):
        self.service.game.board.set_fen("4k3/8/8/8/8/8/8/R3K3 w - - 99 1")
        self.assertFalse(self.service.game.board.is_fifty_moves())
        self.assertTrue(self.service.game.board.can_claim_fifty_moves())
        state = self.service.status(ALICE)
        self.assertTrue(state["gameOver"])
        self.assertEqual(state["outcome"]["reason"], "50-move rule")
        self.assertEqual(state["outcome"]["result"], "1/2-1/2")
        self.assertIsNone(state["outcome"]["winner"])

    def test_http_authorization_and_no_static_exposure(self):
        server = create_server(self.service, TOKEN, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, method="GET", body=None, authenticated=True, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                h = {"X-Player-Session": ALICE}
                if authenticated: h["Authorization"] = "Bearer " + TOKEN
                if headers: h.update(headers)
                conn.request(method, path, body=body, headers=h)
                response = conn.getresponse()
                status, payload = response.status, json.loads(response.read())
                conn.close()
                return status, payload
            status, result = request("/api/status", authenticated=False)
            self.assertEqual(status, 401)
            self.assertFalse(result["ok"])
            self.assertEqual(request("/api/status")[0], 200)
            for path in ["/", "/.env", "/.git/config", "/backend/remote_server.py", "/api/unknown"]:
                self.assertEqual(request(path)[0], 404)
            status, _ = request("/api/new", "POST", "x"*4097, headers={"Content-Type": "application/json"})
            self.assertEqual(status, 413)
            status, _ = request("/api/new", "POST", "[]", headers={"Content-Type": "application/json"})
            self.assertEqual(status, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join()


class OnlineGateTests(unittest.TestCase):
    """Website games run only while the board's screen has Online selected and keeps saying so."""

    SCREEN = SCREEN

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000]
        self.engine = FakeEngine()
        self.service = GameService(Path(self.temp.name) / "state.sqlite3", lambda: self.engine, lambda: self.now[0], require_online=True)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def command(self, route, **fields):
        state = self.service.status(ALICE)
        self.now[0] += 1
        return self.service.mutate(route, {"requestId": f"o-{self.now[0]}-{route.rsplit('/', 1)[1]}", "expectedGameId": state["gameId"],
                                           "expectedRevision": state["revision"], **fields}, ALICE)

    def heartbeat(self, active=True):
        return self.service.mutate("/api/online", {"active": active}, self.SCREEN)

    def test_nothing_starts_until_the_screen_selects_online(self):
        self.assertFalse(self.service.status(ALICE)["online"])
        with self.assertRaises(ApiError) as caught:
            self.command("/api/new")
        self.assertEqual(caught.exception.code, "board_offline")
        self.assertIn("Tap Online", caught.exception.message)
        shown = self.heartbeat()
        self.assertTrue(shown["online"])
        self.assertEqual(shown["clients"], 1, "the screen sees the browser that just asked")
        self.command("/api/new")
        self.assertEqual(len(self.command("/api/move", **{"from": "e2", "to": "e4"})["uciMoves"]), 2)

    def test_online_ends_when_the_heartbeat_stops_or_says_so(self):
        self.heartbeat()
        self.now[0] += 11
        self.assertFalse(self.service.status(ALICE)["online"], "no heartbeat for 11 s")
        self.heartbeat()
        self.assertTrue(self.service.status(ALICE)["online"])
        self.heartbeat(active=False)
        self.assertFalse(self.service.status(ALICE)["online"], "the screen left Online")
        with self.assertRaises(ApiError) as caught:
            self.command("/api/new")
        self.assertEqual(caught.exception.code, "board_offline")

    def test_status_still_answers_while_offline_so_the_website_can_say_why(self):
        state = self.service.status(ALICE)
        self.assertEqual((state["online"], state["ok"]), (False, True))

    def test_ending_a_game_goes_through_the_same_gate_as_a_move(self):
        self.heartbeat()
        self.command("/api/new")
        self.heartbeat(active=False)
        with self.assertRaises(ApiError) as caught:
            self.command("/api/end", reason="resignation", loser="w")
        self.assertEqual(caught.exception.code, "board_offline")
        self.heartbeat()
        self.assertTrue(self.command("/api/end", reason="resignation", loser="w")["gameOver"])


class RecordingDriver:
    """Stands in for the gantry program, and writes down the stages as they happen."""
    name = "test"
    moves_pieces, needs_person = True, False

    def __init__(self, events=None):
        self.jobs, self.fail, self.gate, self.events = [], False, None, events if events is not None else []

    def execute(self, job):
        self.events.append(("gantry", job["label"]))
        if self.gate:
            self.gate.wait(5)
        self.jobs.append(job)
        if self.fail:
            raise RuntimeError("gantry jammed")


class FakeCamera:
    """Sees the board the last job promised, or whatever a test says, and writes down that it looked."""

    def __init__(self, driver, events):
        self.driver, self.events, self.seen, self.fail, self.photos = driver, events, None, False, 0

    def observe(self):
        self.photos += 1
        job = self.driver.jobs[-1] if self.driver.jobs else None
        self.events.append(("camera", job["label"] if job else None))
        if self.fail:
            raise OSError("camera unplugged")
        if self.seen is not None:
            return self.seen
        return BoardObservation(frozenset(job["expect"] if job else chess.Board().piece_map()))


class ScriptedEngine(FakeEngine):
    """Replies from a list, writing down when it was asked."""

    def __init__(self, events, replies=()):
        super().__init__()
        self.events, self.replies, self.gate = events, list(replies), None

    def choose_move(self, board, level):
        self.events.append(("stockfish", board.peek().uci()))
        if self.gate:
            self.gate.wait(5)
        if self.replies:
            self.calls += 1
            return chess.Move.from_uci(self.replies.pop(0))
        return super().choose_move(board, level)


class QueueTestCase(unittest.TestCase):
    camera_connected = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.events = []
        self.engine = ScriptedEngine(self.events)
        self.driver = RecordingDriver(self.events)
        self.camera = FakeCamera(self.driver, self.events) if self.camera_connected else None
        self.service = self.start()
        self.seq = 0

    def start(self, driver=None):
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60,
                           motion_driver=driver or self.driver, camera=self.camera)

    def tearDown(self):
        if self.driver.gate:
            self.driver.gate.set()
        if self.engine.gate:
            self.engine.gate.set()
        self.service.close()
        self.temp.cleanup()

    def command(self, route, player=ALICE, **fields):
        if route == "/api/new":
            fields.setdefault("board", True)  # Queue tests are about games played out on the board.
        state = self.service.status(player)
        self.seq += 1
        return self.service.mutate(route, {"requestId": "m-" + str(self.seq), "expectedGameId": state["gameId"],
                                           "expectedRevision": state["revision"], **fields}, player)

    def move(self, source, target, **fields):
        return self.command("/api/move", **{"from": source, "to": target, **fields})

    def resolve(self, player=ALICE):
        return self.service.mutate("/api/motion-resolve", {}, player)

    def wait_for(self, predicate, what):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            motion = self.service.status(ALICE)["motion"]
            if predicate(motion):
                return motion
            time.sleep(0.005)
        self.fail(f"never {what}: {motion}")

    def settled(self, expected="idle"):
        return self.wait_for(lambda m: m["state"] == expected, f"reached {expected}")

    def moves(self):
        return self.service.status(ALICE)["uciMoves"]


class WebsitePipelineTests(QueueTestCase):
    """Player, gantry, camera, Stockfish, gantry, camera, back to the player."""
    camera_connected = True

    def test_every_stage_runs_in_order_and_only_then_is_it_the_players_turn(self):
        self.command("/api/new")
        reply = self.move("e2", "e4")
        self.assertEqual(reply["uciMoves"], ["e2e4"], "Stockfish must not have moved yet")
        self.assertEqual(reply["turn"], "b")
        self.settled()
        played = self.moves()
        self.assertEqual(len(played), 2)
        self.assertEqual(self.events, [("gantry", "e2e4"), ("camera", "e2e4"), ("stockfish", "e2e4"),
                                       ("gantry", played[1]), ("camera", played[1])])
        self.assertEqual(self.service.status(ALICE)["turn"], "w")

    def test_failed_engine_stops_with_the_players_move_safe_and_can_retry(self):
        self.command("/api/new")
        self.engine.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            motion = self.settled("fault")
        self.assertIn("Stockfish", motion["error"])
        self.assertEqual(self.moves(), ["e2e4"])
        with self.assertRaises(ApiError) as caught:
            self.command("/api/undo")
        self.assertEqual(caught.exception.code, "board_fault")
        self.engine.fail = False
        self.resolve()
        self.settled()
        self.assertEqual(len(self.moves()), 2)

    def test_the_gantry_gets_the_right_data(self):
        self.engine.replies = ["d7d5"]
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        human, stockfish = self.driver.jobs
        self.assertEqual(human["label"], "e2e4")
        self.assertEqual(human["steps"], [{"op": "carry", "piece": "P", "from": "e2", "to": "e4", "path": [[4.0, 1.0], [4.0, 3.0]]}])
        self.assertEqual((len(human["before"]), human["pieces"]["e2"]), (32, "P"), "the gantry program gets the board it starts from")
        self.assertEqual(stockfish["pieces"]["e4"], "P")
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(set(human["expect"]), occupied_squares(board))
        self.assertEqual(stockfish["steps"], [{"op": "carry", "piece": "p", "from": "d7", "to": "d5", "path": [[3.0, 6.0], [3.0, 4.0]]}])
        board.push_uci("d7d5")
        self.assertEqual(set(stockfish["expect"]), occupied_squares(board))
        self.engine.replies = ["d8d5"]
        self.move("e4", "d5")
        self.settled()
        capture, recapture = self.driver.jobs[2:]
        self.assertEqual([(s["piece"], s["from"], s["to"]) for s in capture["steps"]], [("p", "d5", "b:0"), ("P", "e4", "d5")])
        self.assertEqual([(s["piece"], s["from"], s["to"]) for s in recapture["steps"]], [("P", "d5", "w:0"), ("q", "d8", "d5")])

    def test_the_player_cannot_move_at_any_stage_until_the_turn_comes_back(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.move("e2", "e4")
        self.assertEqual(self.wait_for(lambda m: m["current"] == "e2e4", "started")["stage"], "gantry")
        for attempt in (lambda: self.move("d2", "d4"), lambda: self.command("/api/undo"), lambda: self.command("/api/new")):
            with self.assertRaises(ApiError) as caught:
                attempt()
            self.assertEqual(caught.exception.code, "board_moving")
        self.engine.gate = threading.Event()
        self.driver.gate.set()
        self.driver.gate = None
        self.assertEqual(self.wait_for(lambda m: m["stage"] == "stockfish", "thinking")["state"], "moving")
        self.assertEqual(self.moves(), ["e2e4"])
        with self.assertRaises(ApiError) as caught:
            self.move("d2", "d4")
        self.assertEqual(caught.exception.code, "board_moving")
        self.engine.gate.set()
        self.settled()
        self.move("d2", "d4")
        self.settled()
        self.assertEqual(len(self.moves()), 4)

    def test_camera_rejecting_the_players_move_stops_before_stockfish(self):
        self.command("/api/new")
        self.camera.seen = BoardObservation(frozenset({"a1"}))
        with self.assertLogs(level="WARNING"):
            self.move("e2", "e4")
            motion = self.settled("fault")
        self.assertIn("camera", motion["error"])
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.moves(), ["e2e4"])
        with self.assertRaises(ApiError) as caught:
            self.move("d2", "d4")
        self.assertEqual(caught.exception.code, "board_fault")
        # Someone fixes the pieces and confirms: the game carries on from where it stopped.
        self.camera.seen = None
        self.resolve()
        self.settled()
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(len(self.moves()), 2)

    def test_camera_rejecting_stockfishs_move_does_not_give_the_turn_back(self):
        self.command("/api/new")
        self.engine.replies = ["e7e5"]
        real = self.camera.observe
        def observe():
            seen = real()
            return BoardObservation(frozenset({"a1"})) if len(self.driver.jobs) == 2 else seen
        self.camera.observe = observe
        with self.assertLogs(level="WARNING"):
            self.move("e2", "e4")
            motion = self.settled("fault")
        self.assertEqual(motion["current"], "e7e5")
        self.assertEqual(self.moves(), ["e2e4", "e7e5"])
        with self.assertRaises(ApiError) as caught:
            self.move("d2", "d4")
        self.assertEqual(caught.exception.code, "board_fault")
        self.camera.observe = real
        self.resolve()
        self.settled()
        self.move("d2", "d4")
        self.settled()
        self.assertEqual(len(self.moves()), 4)

    def test_a_game_ended_by_the_players_move_never_asks_stockfish(self):
        self.engine.replies = ["e7e5", "b8c6", "a7a6"]
        self.command("/api/new")
        for source, target in [("e2", "e4"), ("f1", "c4"), ("d1", "h5")]:
            self.move(source, target)
            self.settled()
        asked = self.engine.calls
        self.assertFalse(self.service.status(BOB)["canControl"])
        reply = self.move("h5", "f7")
        self.assertTrue(reply["gameOver"])
        self.assertEqual(reply["outcome"]["reason"], "checkmate")
        self.settled()
        self.assertEqual(self.engine.calls, asked)
        # The mating move finishes on the board, then the pieces go home and the camera checks the start.
        self.assertEqual([job["label"] for job in self.driver.jobs[-2:]], ["h5f7", "reset"])
        self.assertEqual(self.events[-2:], [("gantry", "reset"), ("camera", "reset")])
        self.assertEqual(set(self.driver.jobs[-1]["expect"]), occupied_squares(chess.Board()))
        self.assertEqual(self.service.record["boardMoves"], [])
        self.assertTrue(self.service.status(BOB)["canControl"], "the lease ends with the game")
        state = self.service.status(ALICE)
        self.assertEqual((state["started"], state["gameOver"], len(state["uciMoves"])), (True, True, 7), "the finished game stays on screen")

    def test_stockfish_mating_the_player_still_finishes_on_the_board(self):
        self.engine.replies = ["e7e5", "d8h4"]
        self.command("/api/new")
        self.move("f2", "f3")
        self.settled()
        self.move("g2", "g4")
        self.settled()
        state = self.service.status(ALICE)
        self.assertTrue(state["gameOver"])
        self.assertEqual(state["outcome"]["winner"], "b")
        self.assertEqual([job["label"] for job in self.driver.jobs[-2:]], ["d8h4", "reset"])
        self.driver.jobs.clear()
        self.command("/api/new")
        self.settled()
        self.assertEqual(self.driver.jobs, [], "the next game finds the board already set up")

    def test_takeback_runs_through_the_gantry_and_camera_with_no_new_reply(self):
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        reply = self.moves()[1]
        asked, self.events[:] = self.engine.calls, []
        self.command("/api/undo")
        self.settled()
        self.assertEqual(self.events, [("gantry", "undo " + reply), ("camera", "undo " + reply), ("gantry", "undo e2e4"), ("camera", "undo e2e4")])
        self.assertEqual(self.engine.calls, asked)
        self.assertEqual(self.moves(), [])

    def test_a_move_read_off_the_board_skips_the_gantry_for_that_move_only(self):
        self.command("/api/new")
        self.move("g1", "f3", fromBoard=True)
        self.settled()
        self.assertEqual(self.events[0], ("stockfish", "g1f3"))
        self.assertEqual(len(self.driver.jobs), 1)

    def test_a_restart_during_the_gantry_never_assumes_it_finished(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.move("e2", "e4")
        self.wait_for(lambda m: self.events, "started")
        self.service.close()
        self.service = self.start()
        motion = self.service.status(ALICE)["motion"]
        self.assertEqual(motion["state"], "fault")
        self.assertIn("restarted", motion["error"])


class NothingConnectedTests(QueueTestCase):
    """The Pi today: no gantry program and no camera. The game must still work and log the data."""

    def start(self, driver=None):
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60, motion_driver=driver)

    def test_the_game_runs_end_to_end_and_logs_what_it_would_send(self):
        self.command("/api/new")
        with self.assertLogs(level="INFO") as logged:
            self.move("e2", "e4")
            self.settled()
        output = "\n".join(logged.output)
        self.assertIn('"from": "e2", "to": "e4"', output)
        self.assertIn("gantry: not connected", output)
        self.assertIn("camera: skipped", output)
        self.assertIn("stockfish: played", output)
        self.assertIn("back to the player", output)
        state = self.service.status(ALICE)
        self.assertEqual(len(state["uciMoves"]), 2)
        self.assertEqual(state["motion"]["mode"], "simulated")
        self.assertEqual(state["motion"]["state"], "idle")
        self.assertFalse(state["motion"]["camera"])

    def test_a_connected_camera_is_not_consulted_while_the_gantry_cannot_have_moved_anything(self):
        self.service.close()
        camera = FakeCamera(self.driver, self.events)
        self.service = GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60, camera=camera)
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(camera.photos, 0)
        self.assertEqual(len(self.moves()), 2)


class GantryFailureTests(QueueTestCase):
    def test_gantry_failure_stops_before_stockfish_and_blocks_moves_until_someone_checks(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            motion = self.settled("fault")
        self.assertEqual((motion["current"], motion["pending"]), ("e2e4", 0))
        self.assertNotIn("jammed", motion["error"])
        self.assertEqual(self.engine.calls, 0)
        revision = self.service.status(ALICE)["revision"]
        with self.assertRaises(ApiError) as caught:
            self.move("d2", "d4")
        self.assertEqual(caught.exception.code, "board_fault")
        self.assertEqual(self.service.status(ALICE)["revision"], revision)
        with self.assertRaises(ApiError) as caught:
            self.resolve(BOB)
        self.assertEqual(caught.exception.code, "board_busy")
        self.driver.fail = False
        self.resolve()
        self.settled()
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(len(self.moves()), 2)

    def test_resolving_without_a_fault_changes_nothing(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.move("e2", "e4")
        self.resolve()
        self.driver.gate.set()
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2)

    def test_new_game_after_moves_sends_the_pieces_home_and_on_an_untouched_board_sends_nothing(self):
        self.command("/api/new")
        self.settled()
        self.assertEqual(self.driver.jobs, [])
        self.move("e2", "e4")
        self.settled()
        self.driver.jobs.clear()
        self.command("/api/new")
        self.settled()
        self.assertEqual([job["label"] for job in self.driver.jobs], ["reset"])

    def test_unreadable_saved_step_stops_the_queue_with_a_message_instead_of_hanging(self):
        self.command("/api/new")
        board = self.service.board
        with board.ready:
            with board.db:
                board.db.execute("INSERT INTO motion_jobs (label, steps, expect, status) VALUES ('bad', 'not json', '[]', 'pending')")
            board.ready.notify()
        with self.assertLogs(level="ERROR"):
            motion = self.settled("fault")
        self.assertIn("could not be read", motion["error"])
        self.assertTrue(board.thread.is_alive())
        self.assertEqual(self.resolve()["motion"]["state"], "idle")
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2)

    def test_worker_survives_an_unexpected_error(self):
        board = self.service.board
        real, calls = board._next_job, []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk full")
            return real()
        board._next_job = flaky
        self.command("/api/new")
        with self.assertLogs(level="ERROR"), board.ready:
            board.ready.notify()
            board.ready.wait(0.2)
        self.assertTrue(board.thread.is_alive())
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2)

    def test_server_id_survives_a_restart_but_differs_between_databases(self):
        first = self.service.status(ALICE)["serverId"]
        self.service.close()
        self.service = self.start()
        self.assertEqual(self.service.status(ALICE)["serverId"], first)
        with tempfile.TemporaryDirectory() as other:
            fresh = GameService(Path(other) / "state.sqlite3", lambda: self.engine, lambda: 1000)
            try:
                self.assertNotEqual(fresh.status(ALICE)["serverId"], first)
            finally:
                fresh.close()


class PersonMovesPiecesTests(QueueTestCase):
    """CHESS_BOARD_MODE=manual: the same order, with a person in the gantry's place."""
    camera_connected = True

    def start(self, driver=None):
        self.manual = ManualDriver()
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60,
                           motion_driver=driver or self.manual, camera=self.camera)

    def setUp(self):
        super().setUp()
        self.camera = FakeCamera(self.driver, self.events)
        self.service.board.camera = self.camera
        self.true_board = lambda: BoardObservation(frozenset(self.driver.jobs and () or occupied_squares(self.service.game.board)))
        self.camera.observe = lambda: self.seen_override or self.true_board()
        self.seen_override = None

    def confirm(self, **body):
        return self.service.mutate("/api/board-confirm", body, ALICE)

    def waiting_on(self, label):
        return self.wait_for(lambda m: m["state"] == "waiting" and m["current"] == label, f"waited on {label}")

    def new_game(self):
        self.command("/api/new")
        self.waiting_on("reset")
        self.confirm(override=True)
        self.settled()

    def reply_uci(self):
        deadline = time.monotonic() + 5
        while len(self.moves()) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.moves()[1]

    def test_new_game_checks_the_starting_board_first(self):
        self.seen_override = BoardObservation(frozenset({"a1"}))
        self.command("/api/new")
        self.assertEqual(self.waiting_on("reset")["instructions"], ["Set up the standard starting position."])
        self.confirm()
        motion = self.wait_for(lambda m: m["mismatch"], "reported a mismatch")
        self.assertEqual(motion["state"], "waiting")
        self.seen_override = None
        self.confirm()
        self.settled()

    def test_stockfish_waits_until_the_person_has_moved_the_players_piece(self):
        self.new_game()
        reply = self.move("e2", "e4")
        self.assertEqual(reply["uciMoves"], ["e2e4"])
        self.assertEqual(reply["motion"]["state"], "waiting")
        self.assertEqual(reply["motion"]["instructions"], ["Move the white pawn from e2 to e4."])
        self.assertIsInstance(reply["motion"]["jobId"], int)
        time.sleep(0.05)
        self.assertEqual(self.engine.calls, 0)
        self.confirm()
        answer = self.reply_uci()
        self.waiting_on(answer)
        self.assertEqual(self.engine.calls, 1)
        self.confirm()
        self.settled()

    def test_a_press_for_an_earlier_step_cannot_confirm_the_next_one(self):
        self.new_game()
        self.move("e2", "e4")
        first = self.waiting_on("e2e4")["jobId"]
        self.confirm(jobId=first)
        answer = self.reply_uci()
        second = self.waiting_on(answer)["jobId"]
        self.assertNotEqual(first, second)
        self.confirm(jobId=first)
        time.sleep(0.1)
        self.assertEqual(self.waiting_on(answer)["jobId"], second)
        self.confirm(jobId=second)
        self.settled()

    def test_override_accepts_the_board_without_the_camera(self):
        self.new_game()
        self.seen_override = BoardObservation(frozenset({"a1"}))
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        self.confirm(override=True)
        self.waiting_on(self.reply_uci())

    def test_wrong_board_and_broken_camera_ask_again_instead_of_failing(self):
        self.new_game()
        self.seen_override = BoardObservation(frozenset(), ("d4",))
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        self.confirm()
        self.assertEqual(self.wait_for(lambda m: m["mismatch"], "reported")["mismatch"]["uncertain"], ["d4"])
        def broken():
            raise OSError("camera unplugged")
        self.camera.observe = broken
        with self.assertLogs(level="ERROR"):
            self.confirm()
            motion = self.wait_for(lambda m: (m["mismatch"] or {}).get("error"), "reported the camera")
        self.assertEqual(motion["state"], "waiting")
        self.assertNotIn("unplugged", motion["mismatch"]["error"])

    def test_restart_while_waiting_asks_the_person_again(self):
        self.new_game()
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        self.service.close()
        self.service = self.start()
        self.assertEqual(self.waiting_on("e2e4")["instructions"], ["Move the white pawn from e2 to e4."])

    def test_new_game_clears_steps_nobody_has_done_yet(self):
        self.new_game()
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        self.command("/api/new")
        motion = self.waiting_on("reset")
        self.assertEqual((motion["pending"], motion["instructions"]), (0, ["Set up the standard starting position."]))
        self.confirm(override=True)
        self.settled()
        self.assertEqual(self.moves(), [])

    def test_a_press_right_after_a_new_game_never_strands_the_queue(self):
        for _ in range(25):
            self.new_game()
            self.move("e2", "e4")
            self.waiting_on("e2e4")
            self.command("/api/new")
            self.confirm(override=True)
            self.settled()

    def test_fresh_and_reset_from_the_screen_release_a_step_nobody_has_done(self):
        self.new_game()
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        shown = self.service.mutate("/api/online", {"active": True, "fresh": True}, SCREEN)
        self.assertEqual((shown["started"], shown["motion"]["state"]), (False, "idle"))
        self.settled()
        self.new_game()
        self.move("e2", "e4")
        self.waiting_on("e2e4")
        shown = self.service.mutate("/api/online", {"active": True, "reset": True}, SCREEN)
        motion = shown["motion"]
        self.assertEqual((shown["gameOver"], motion["state"], motion["current"], motion["pending"]), (True, "waiting", "reset", 0))
        self.assertEqual(motion["instructions"], ["Set up the standard starting position."])
        self.confirm(override=True)
        self.settled()
        self.assertEqual(self.moves(), ["e2e4"], "the ended game stays on screen")


class DigitalGameTests(QueueTestCase):
    """The regular website game: Stockfish answers in the request, and the board hardware is never touched."""
    camera_connected = True

    def test_nothing_physical_happens_and_the_reply_comes_back_at_once(self):
        self.command("/api/new", board=False)
        reply = self.move("e2", "e4")
        self.assertFalse(reply["board"])
        self.assertEqual(len(reply["uciMoves"]), 2)
        self.assertEqual(reply["computer"]["uci"], reply["uciMoves"][1])
        self.assertEqual(reply["turn"], "w")
        self.assertEqual(reply["motion"]["state"], "idle")
        time.sleep(0.05)
        self.assertEqual([event for event in self.events if event[0] != "stockfish"], [])
        self.assertEqual(self.driver.jobs, [])
        self.assertEqual(self.camera.photos, 0)
        self.command("/api/undo")
        self.command("/api/new", board=False)
        time.sleep(0.05)
        self.assertEqual(self.driver.jobs, [], "takebacks and new games move nothing either")

    def test_a_stopped_board_does_not_stop_the_digital_game_but_does_stop_board_games(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            self.settled("fault")
        self.command("/api/new", board=False)
        self.assertEqual(len(self.move("d2", "d4")["uciMoves"]), 2)
        self.assertEqual(self.service.status(ALICE)["motion"]["state"], "fault", "the hardware still needs checking")
        with self.assertRaises(ApiError) as caught:
            self.command("/api/new", board=True)
        self.assertEqual(caught.exception.code, "board_fault")
        self.driver.fail = False
        self.resolve()
        self.command("/api/new", board=True)
        self.settled()

    def test_a_board_game_after_digital_games_resets_from_where_the_pieces_really_are(self):
        self.engine.replies = ["e7e5"]
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(self.moves(), ["e2e4", "e7e5"])
        self.command("/api/new", board=False)
        self.engine.replies = ["g8f6", "b8c6"]
        self.move("g1", "f3")
        self.move("b1", "c3")
        self.assertEqual(len(self.moves()), 4)
        self.driver.jobs.clear()
        self.command("/api/new", board=True)
        self.settled()
        reset, = self.driver.jobs
        self.assertEqual(reset["label"], "reset")
        moved_from = {step["from"] for step in reset["steps"] if ":" not in step["from"]}
        self.assertEqual(moved_from, {"e4", "e5"}, "only the pieces the board game moved go home; the digital knights never left")

    def test_reading_the_board_is_only_for_board_games(self):
        self.command("/api/new", board=False)
        with self.assertRaises(ApiError) as caught:
            self.service.mutate("/api/board-scan", {}, ALICE)
        self.assertEqual(caught.exception.code, "not_board_game")

    def test_a_game_saved_before_the_choice_existed_is_a_board_game(self):
        self.service.close()
        db = sqlite3.connect(self.path)
        record = json.loads(db.execute("SELECT value FROM state").fetchone()[0])
        for key in ("board", "boardMoves"):
            record.pop(key, None)
        record.update(started=True, uciMoves=["e2e4"])
        with db:
            db.execute("UPDATE state SET value=?", (json.dumps(record),))
        db.close()
        self.service = self.start()
        self.assertTrue(self.service.status(ALICE)["board"])
        self.settled()
        self.assertEqual(len(self.moves()), 2)
        self.assertEqual(self.driver.jobs[-1]["label"], self.moves()[1])


class GameEndTests(QueueTestCase):
    """A game ends by the rules, by /api/end from the website, or by End Game on the screen; the board then resets itself.

    Nothing ends a game on a timer. The screen's fresh replaces whatever game was on with an untouched board.
    """
    camera_connected = True

    def screen(self, **body):
        return self.service.mutate("/api/online", {"active": True, **body}, SCREEN)

    def end(self, reason="timeout", loser="w", player=ALICE):
        return self.command("/api/end", player, reason=reason, loser=loser)

    def play_opening(self):
        self.engine.replies = ["e7e5"]
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(self.moves(), ["e2e4", "e7e5"])

    def test_a_clock_at_zero_ends_the_game_and_the_board_puts_the_pieces_back(self):
        self.play_opening()
        self.events.clear()
        reply = self.end("timeout", "w")
        self.assertEqual((reply["started"], reply["gameOver"]), (True, True))
        self.assertEqual(reply["outcome"], {"result": "0-1", "winner": "b", "reason": "timeout"})
        self.assertEqual(reply["result"], "Black wins on time.")
        self.assertEqual(reply["ended"], {"reason": "timeout", "winner": "b", "result": "0-1", "text": "Black wins on time."})
        self.assertEqual(reply["uciMoves"], ["e2e4", "e7e5"], "the finished game stays on screen")
        self.assertEqual((reply["motion"]["state"], reply["motion"]["current"]), ("moving", "reset"))
        self.settled()
        self.assertEqual(self.events, [("gantry", "reset"), ("camera", "reset")], "the reset is camera-checked and Stockfish is not asked")
        reset = self.driver.jobs[-1]
        self.assertEqual(set(reset["expect"]), occupied_squares(chess.Board()))
        self.assertEqual({step["from"] for step in reset["steps"] if ":" not in step["from"]}, {"e4", "e5"})
        self.assertEqual((self.service.record["owner"], self.service.record["boardMoves"]), (None, []))
        state = self.service.status(BOB)
        self.assertEqual((state["canControl"], state["gameOver"]), (True, True))
        self.driver.jobs.clear()
        self.command("/api/new", BOB)
        self.settled()
        self.assertEqual(self.driver.jobs, [], "the board is already set up: no second reset")
        state = self.service.status(BOB)
        self.assertEqual((state["gameOver"], state["ended"], state["outcome"]), (False, None, None))

    def test_a_resignation_names_the_winner_and_is_deduplicated(self):
        self.play_opening()
        state = self.service.status(ALICE)
        body = {"requestId": "end-once", "expectedGameId": state["gameId"], "expectedRevision": state["revision"],
                "reason": "resignation", "loser": "b"}
        reply = self.service.mutate("/api/end", body, ALICE)
        self.assertEqual((reply["outcome"]["winner"], reply["outcome"]["result"], reply["result"]), ("w", "1-0", "White wins by resignation."))
        self.assertEqual(self.service.mutate("/api/end", body, ALICE), reply, "a retry replays the same answer")
        self.settled()
        self.assertEqual([job["label"] for job in self.driver.jobs if job["label"] == "reset"], ["reset"])
        for attempt in (lambda: self.end("resignation", "w"), lambda: self.move("d2", "d4"), lambda: self.command("/api/undo")):
            with self.assertRaises(ApiError) as caught:
                attempt()
            self.assertEqual(caught.exception.code, "game_over")
        self.assertEqual(self.service.status(BOB)["result"], "White wins by resignation.", "a viewer sees how it ended")

    def test_white_resigning_loses(self):
        self.play_opening()
        reply = self.end("resignation", "w")
        self.assertEqual((reply["outcome"], reply["result"]), ({"result": "0-1", "winner": "b", "reason": "resignation"}, "Black wins by resignation."))
        self.settled()

    def test_end_refuses_bad_reasons_and_games_that_have_not_started(self):
        with self.assertRaises(ApiError) as caught:
            self.end("timeout", "w")
        self.assertEqual(caught.exception.code, "not_started")
        self.command("/api/new")
        for reason, loser in (("abandoned", "w"), ("timeout", "x"), (None, "w")):
            with self.assertRaises(ApiError) as caught:
                self.end(reason, loser)
            self.assertEqual(caught.exception.code, "invalid_end")
        self.assertFalse(self.service.status(ALICE)["gameOver"])

    def test_a_digital_game_ends_without_touching_the_board_and_frees_the_lease(self):
        self.command("/api/new", board=False)
        self.move("e2", "e4")
        self.assertFalse(self.service.status(BOB)["canControl"])
        reply = self.end("timeout", "w")
        self.assertEqual((reply["gameOver"], reply["board"], reply["motion"]["state"]), (True, False, "idle"))
        time.sleep(0.05)
        self.assertEqual(self.driver.jobs, [])
        self.assertEqual(self.camera.photos, 0)
        self.assertIsNone(self.service.record["owner"])
        self.assertTrue(self.service.status(BOB)["canControl"])

    def test_a_game_ended_while_the_board_is_still_moving_finishes_that_move_then_resets(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.move("e2", "e4")
        self.wait_for(lambda m: m["current"] == "e2e4", "started")
        self.assertTrue(self.end("timeout", "w")["gameOver"], "a clock does not wait for the gantry")
        self.driver.gate.set()
        self.driver.gate = None
        self.settled()
        self.assertEqual([job["label"] for job in self.driver.jobs], ["e2e4", "reset"])
        self.assertEqual(self.engine.calls, 0, "Stockfish owes nothing to a finished game")
        self.assertEqual(self.moves(), ["e2e4"])

    def test_a_game_ended_while_stockfish_thinks_drops_its_reply_even_across_a_restart(self):
        self.command("/api/new")
        self.engine.gate = threading.Event()
        self.move("e2", "e4")
        self.wait_for(lambda m: m["stage"] == "stockfish", "thinking")
        self.end("resignation", "w")
        self.engine.gate.set()
        self.engine.gate = None
        self.settled()
        self.assertEqual(self.moves(), ["e2e4"])
        self.assertEqual([job["label"] for job in self.driver.jobs], ["e2e4", "reset"])
        self.service.close()
        self.service = self.start()
        self.settled()
        state = self.service.status(ALICE)
        self.assertEqual((state["gameOver"], state["outcome"]["reason"], state["result"], state["uciMoves"]),
                         (True, "resignation", "Black wins by resignation.", ["e2e4"]))
        self.assertEqual(self.engine.calls, 1, "a restart does not ask Stockfish to continue a finished game")

    def test_a_stopped_board_must_be_checked_before_the_game_can_end(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            self.settled("fault")
        with self.assertRaises(ApiError) as caught:
            self.end("timeout", "w")
        self.assertEqual(caught.exception.code, "board_fault")

    def test_a_game_saved_before_endings_existed_loads_as_unfinished(self):
        self.command("/api/new", board=False)
        self.service.close()
        db = sqlite3.connect(self.path)
        record = json.loads(db.execute("SELECT value FROM state").fetchone()[0])
        del record["ended"]
        with db:
            db.execute("UPDATE state SET value=?", (json.dumps(record),))
        db.close()
        self.service = self.start()
        state = self.service.status(ALICE)
        self.assertEqual((state["gameOver"], state["ended"]), (False, None))

    def test_fresh_from_the_screen_replaces_the_old_game_with_an_untouched_board(self):
        self.play_opening()
        before = self.service.status(BOB)
        self.assertFalse(before["canControl"], "Alice's game is still on")
        shown = self.screen(fresh=True)
        self.assertEqual((shown["started"], shown["uciMoves"], shown["board"], shown["gameOver"], shown["ended"]),
                         (False, [], True, False, None))
        self.assertEqual((shown["fen"], shown["leaseExpiresAt"]), (chess.Board().fen(), 0))
        self.assertNotEqual(shown["gameId"], before["gameId"])
        self.assertEqual(shown["revision"], before["revision"] + 1)
        self.assertEqual(shown["motion"]["state"], "idle", "the pieces are already home: nothing is queued")
        time.sleep(0.05)
        self.assertEqual([job["label"] for job in self.driver.jobs], ["e2e4", "e7e5"], "nothing moved for the fresh board")
        self.assertEqual((self.service.record["owner"], self.service.record["boardMoves"]), (None, []))
        self.assertTrue(self.service.status(BOB)["canControl"])
        with self.assertRaises(ApiError) as caught:
            self.service.mutate("/api/move", {"requestId": "old-tab", "expectedGameId": before["gameId"],
                                              "expectedRevision": before["revision"], "from": "d2", "to": "d4"}, ALICE)
        self.assertEqual(caught.exception.code, "stale_state", "Alice's open tab learns her game is gone")
        self.command("/api/new", BOB)
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2, "a new game on a fresh board plans no reset")

    def test_fresh_is_honoured_mid_game_and_forgets_a_stopped_board(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            self.settled("fault")
        self.driver.fail = False
        shown = self.screen(fresh=True)
        self.assertEqual((shown["started"], shown["motion"]["state"]), (False, "idle"))
        self.command("/api/new")
        self.move("d2", "d4")
        self.settled()
        self.assertEqual(self.moves()[0], "d2d4")

    def test_a_plain_heartbeat_changes_nothing(self):
        self.play_opening()
        before = copy.deepcopy(self.service.record)
        self.screen()
        self.assertEqual(self.service.record, before)

    def test_end_game_on_the_screen_ends_a_running_game_and_sends_the_pieces_home(self):
        self.play_opening()
        self.events.clear()
        shown = self.screen(reset=True)
        self.assertEqual((shown["started"], shown["gameOver"]), (True, True))
        self.assertEqual(shown["outcome"], {"result": None, "winner": None, "reason": "abandoned"})
        self.assertEqual(shown["result"], "Game ended from the board.")
        self.assertEqual(shown["ended"]["text"], "Game ended from the board.")
        self.assertEqual((shown["motion"]["state"], shown["motion"]["current"]), ("moving", "reset"))
        self.assertEqual(shown["uciMoves"], ["e2e4", "e7e5"])
        self.settled()
        self.assertEqual(self.events, [("gantry", "reset"), ("camera", "reset")])
        self.assertEqual((self.service.record["owner"], self.service.record["boardMoves"]), (None, []))
        self.assertTrue(self.service.status(BOB)["canControl"])
        with self.assertRaises(ApiError) as caught:
            self.move("d2", "d4")
        self.assertEqual(caught.exception.code, "game_over")
        self.driver.jobs.clear()
        self.command("/api/new", BOB)
        self.settled()
        self.assertEqual(self.driver.jobs, [], "no second reset for the new game")

    def test_reset_on_the_screen_clears_a_stopped_board_and_wins_over_fresh(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            self.settled("fault")
        self.driver.fail = False
        self.events.clear()
        shown = self.screen(reset=True, fresh=True)
        self.assertEqual((shown["gameOver"], shown["started"], shown["result"]), (True, True, "Game ended from the board."))
        self.settled()
        self.assertEqual(self.events, [("gantry", "reset"), ("camera", "reset")])
        self.assertEqual({s["from"] for s in self.driver.jobs[-1]["steps"] if ":" not in s["from"]}, {"e4"},
                         "planned from where the board was last sent")

    def test_reset_with_nothing_to_put_back_still_lets_the_camera_check_a_played_board(self):
        before = self.service.status(ALICE)
        shown = self.screen(reset=True)
        self.assertEqual((shown["started"], shown["gameOver"], shown["ended"], shown["revision"], shown["motion"]["state"]),
                         (False, False, None, before["revision"] + 1, "idle"))
        self.play_opening()
        self.end("timeout", "w")
        self.settled()
        self.events.clear()
        shown = self.screen(reset=True)
        self.assertEqual(shown["result"], "Black wins on time.", "a finished game is not ended again")
        self.settled()
        self.assertEqual(self.events, [("gantry", "reset"), ("camera", "reset")])
        self.assertEqual(self.driver.jobs[-1]["steps"], [])


FAKE_GANTRY_DRIVER = """import json, pathlib, sys
job = json.load(sys.stdin)
with pathlib.Path(__file__).with_name("jobs.log").open("a") as log:
    log.write(json.dumps({"args": sys.argv[1:], "label": job["label"], "steps": len(job["steps"])}) + "\\n")
sys.exit(0 if "--quiet" in sys.argv else 2)
"""


class GantryDriverTests(QueueTestCase):
    """CHESS_GANTRY=arduino runs gantry_driver.py from the gantry branch when it is configured, else a placeholder that says so."""

    def gantry_from(self, **settings):
        env = {"CHESS_MOTION_COMMAND": "", "CHESS_GANTRY": "arduino", "CHESS_GANTRY_PORT": "", "CHESS_GANTRY_DIR": "",
               "CHESS_GANTRY_CONFIG": "", "CHESS_GANTRY_PYTHON": "", **settings}
        with mock.patch.dict(os.environ, env):
            return driver_from_env()

    def checkout(self):
        directory = Path(self.temp.name) / "gantry" / "pi"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gantry_driver.py").write_text(FAKE_GANTRY_DRIVER)
        return directory

    def start(self, driver=None):
        if driver is None:
            with self.assertLogs(level="WARNING"):
                driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB7", CHESS_GANTRY_BAUD="9600",
                                          CHESS_GANTRY_DIR=str(Path(self.temp.name) / "nowhere"))
        self.gantry = driver
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60, motion_driver=driver)

    def test_without_a_port_or_the_program_the_placeholder_is_selected(self):
        self.assertIsInstance(self.gantry, ArduinoGantry)
        self.assertEqual((self.gantry.port, self.gantry.baud), ("/dev/ttyUSB7", 9600))
        self.assertEqual(self.service.status(ALICE)["motion"]["mode"], "arduino")
        directory = self.checkout()
        with self.assertLogs(level="WARNING") as logged:
            self.assertIsInstance(self.gantry_from(CHESS_GANTRY_DIR=str(directory)), ArduinoGantry)
        self.assertIn("CHESS_GANTRY_PORT missing", logged.output[0])

    def test_a_board_game_stops_at_the_first_move_and_says_why(self):
        self.command("/api/new")
        with self.assertLogs(level="ERROR"):
            self.move("e2", "e4")
            motion = self.settled("fault")
        self.assertEqual(motion["error"], NOT_DONE)
        self.assertEqual(self.engine.calls, 0)
        self.assertEqual(self.moves(), ["e2e4"])

    def test_the_digital_game_does_not_care(self):
        self.command("/api/new", board=False)
        self.assertEqual(len(self.move("e2", "e4")["uciMoves"]), 2)

    def test_a_port_and_the_checked_out_program_make_the_command(self):
        directory = self.checkout()
        with self.assertLogs(level="INFO"):
            driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB0", CHESS_GANTRY_DIR=str(directory), CHESS_GANTRY_PYTHON="/opt/py")
        self.assertIsInstance(driver, GantryDriver)
        self.assertEqual((driver.name, driver.moves_pieces, driver.needs_person), ("gantry", True, False))
        self.assertEqual(driver.args, ["/opt/py", str(directory / "gantry_driver.py"), "--port", "/dev/ttyUSB0",
                                       "--config", str(directory / "gantry_config.json"), "--quiet"])
        driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB0", CHESS_GANTRY_DIR=str(directory), CHESS_GANTRY_CONFIG="/etc/gantry.json")
        self.assertEqual(driver.args[5], "/etc/gantry.json")
        # Without CHESS_GANTRY_DIR the usual checkouts are tried; the pi-only venv's python is preferred when it exists.
        with mock.patch.object(gantry, "DEFAULT_DIRS", (str(Path(self.temp.name) / "nowhere"), str(directory))), \
                mock.patch.object(gantry, "PI_VENV_PYTHON", str(directory / "gantry_driver.py")):
            driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB0")
        self.assertEqual((driver.directory, driver.python), (directory, str(directory / "gantry_driver.py")))
        with mock.patch.object(gantry, "PI_VENV_PYTHON", str(Path(self.temp.name) / "no-venv")):
            driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB0", CHESS_GANTRY_DIR=str(directory))
        self.assertEqual(driver.python, sys.executable)

    def test_a_board_game_runs_through_the_gantry_program(self):
        self.service.close()
        directory = self.checkout()
        with self.assertLogs(level="INFO"):
            driver = self.gantry_from(CHESS_GANTRY_PORT="/dev/ttyUSB0", CHESS_GANTRY_DIR=str(directory), CHESS_GANTRY_PYTHON=sys.executable)
        self.service = self.start(driver)
        self.assertEqual(self.service.status(ALICE)["motion"]["mode"], "gantry")
        self.engine.replies = ["e7e5"]
        self.command("/api/new")
        self.move("e2", "e4")
        self.settled()
        self.assertEqual(self.moves(), ["e2e4", "e7e5"])
        runs = [json.loads(line) for line in (directory / "jobs.log").read_text().splitlines()]
        self.assertEqual([(run["label"], run["steps"]) for run in runs], [("e2e4", 1), ("e7e5", 1)])
        self.assertEqual(runs[0]["args"], ["--port", "/dev/ttyUSB0", "--config", str(directory / "gantry_config.json"), "--quiet"])


class BoardScanTests(QueueTestCase):
    camera_connected = True

    def scan(self):
        return self.service.mutate("/api/board-scan", {}, ALICE)

    def looks_like(self, *moves):
        board = chess.Board()
        for uci in moves:
            board.push_uci(uci)
        self.camera.seen = BoardObservation(frozenset(occupied_squares(board)))

    def test_scan_finds_the_move_and_commits_nothing(self):
        self.command("/api/new")
        self.settled()
        self.looks_like("g1f3")
        result = self.scan()
        self.assertEqual((result["status"], result["candidates"][0]["san"]), ("matched", "Nf3"))
        self.assertEqual(self.moves(), [])

    def test_scan_reports_nothing_new_and_impossible_boards(self):
        self.command("/api/new")
        self.settled()
        self.looks_like()
        self.assertEqual(self.scan()["status"], "unchanged")
        self.camera.seen = BoardObservation(frozenset(occupied_squares(chess.Board()) - {"a1"}))
        result = self.scan()
        self.assertEqual((result["status"], result["missing"]), ("no_match", ["a1"]))
        self.camera.seen = BoardObservation(frozenset(), ("a1",))
        self.assertEqual(self.scan()["status"], "uncertain")

    def test_scan_refuses_while_the_board_is_busy_or_unavailable(self):
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "not_your_turn")
        self.command("/api/new")
        self.settled()
        self.driver.gate = threading.Event()
        self.move("e2", "e4")
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "board_moving")
        self.driver.gate.set()
        self.settled()
        self.service.close()
        self.service = GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60)
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "no_camera")


if __name__ == "__main__":
    unittest.main()
