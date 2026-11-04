"""Regression tests for authoritative remote game transactions and API exposure."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import chess

from backend.board_camera import BoardObservation, occupied_squares
from backend.board_motion import ManualDriver
from backend.remote_server import ApiError, GameService, create_server

ALICE = "a" * 32
BOB = "b" * 32
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

    def test_duplicate_move_returns_same_reply_exactly_once(self):
        self.command("/api/new", level="beginner")
        first, body = self.command("/api/move", **{"from": "e2", "to": "e4"})
        second = self.service.mutate("/api/move", body, ALICE)
        self.assertEqual(first, second)
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(len(second["uciMoves"]), 2)
        self.assertEqual(second["revision"], 2)

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
        for route in ["/api/new", "/api/move", "/api/undo"]:
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

    def test_failed_engine_and_illegal_moves_do_not_commit(self):
        self.command("/api/new")
        before = copy.deepcopy(self.service.record)
        with self.assertRaises(ApiError):
            self.command("/api/move", **{"from": "e2", "to": "e5"})
        self.assertEqual(self.service.record, before)
        self.engine.fail = True
        with self.assertRaises(ApiError) as caught:
            self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(caught.exception.code, "engine_failed")
        self.assertEqual(self.service.record, before)
        self.assertEqual(self.service.status(ALICE)["uciMoves"], [])

    def test_undo_is_atomic_and_deduplicated(self):
        self.command("/api/new")
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        response, body = self.command("/api/undo")
        self.assertEqual(response["uciMoves"], [])
        self.assertEqual(response["undone"], 2)
        self.assertEqual(self.service.mutate("/api/undo", body, ALICE), response)

    def test_state_and_deduplication_survive_restart(self):
        self.command("/api/new", level="advanced")
        response, body = self.command("/api/move", **{"from": "d2", "to": "d4"})
        self.service.close()
        self.service = GameService(self.path, lambda: self.engine, lambda: self.now[0], lease_seconds=60)
        actual = self.service.status(ALICE)
        for key in ["fen", "uciMoves", "sanMoves", "revision", "gameId", "level", "leaseExpiresAt"]:
            self.assertEqual(actual[key], response[key])
        self.assertEqual(self.service.mutate("/api/move", body, ALICE), response)
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


class RecordingDriver:
    name = "test"
    moves_pieces, needs_person = True, False

    def __init__(self):
        self.jobs, self.fail, self.gate = [], False, None

    def execute(self, job):
        if self.gate:
            self.gate.wait(5)
        self.jobs.append(job)
        if self.fail:
            raise RuntimeError("motor stalled")


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.engine = FakeEngine()
        self.driver = RecordingDriver()
        self.service = self.start()
        self.seq = 0

    def start(self):
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60, motion_driver=self.driver)

    def tearDown(self):
        if self.driver.gate:
            self.driver.gate.set()
        self.service.close()
        self.temp.cleanup()

    def command(self, route, player=ALICE, **fields):
        state = self.service.status(player)
        self.seq += 1
        return self.service.mutate(route, {"requestId": "m-" + str(self.seq), "expectedGameId": state["gameId"],
                                           "expectedRevision": state["revision"], **fields}, player)

    def settled(self, expected="idle"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            motion = self.service.status(ALICE)["motion"]
            if motion["state"] == expected:
                return motion
            time.sleep(0.005)
        self.fail(f"motion never reached {expected}: {motion}")


class MotionQueueTests(QueueTestCase):
    def test_human_and_engine_moves_are_signalled_in_order(self):
        self.command("/api/new")
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(self.settled()["mode"], "test")
        self.assertEqual([job["label"] for job in self.driver.jobs], ["e2e4", reply["computer"]["uci"]])
        self.assertEqual(self.driver.jobs[0]["steps"][0]["path"], [[4, 1], [4, 3]])

    def test_reply_does_not_wait_for_the_pieces_and_reports_the_queue(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertEqual(reply["uciMoves"][0], "e2e4")
        self.assertEqual(reply["motion"]["state"], "moving")
        self.assertEqual(reply["motion"]["current"], "e2e4")
        self.driver.gate.set()
        self.settled()

    def test_failure_stops_the_queue_and_blocks_moves_until_someone_checks_the_board(self):
        self.command("/api/new")
        self.driver.fail = True
        with self.assertLogs(level="ERROR"):
            self.command("/api/move", **{"from": "e2", "to": "e4"})
            motion = self.settled("fault")
        self.assertEqual(len(self.driver.jobs), 1)
        self.assertEqual(motion["current"], "e2e4")
        self.assertEqual(motion["pending"], 1)
        self.assertNotIn("motor stalled", motion["error"])
        revision = self.service.status(ALICE)["revision"]
        with self.assertRaises(ApiError) as caught:
            self.command("/api/move", **{"from": "d2", "to": "d4"})
        self.assertEqual(caught.exception.code, "board_fault")
        self.assertEqual(self.service.status(ALICE)["revision"], revision)
        with self.assertRaises(ApiError) as caught:
            self.service.mutate("/api/motion-resolve", {}, BOB)
        self.assertEqual(caught.exception.code, "board_busy")
        self.driver.fail = False
        self.assertEqual(self.service.mutate("/api/motion-resolve", {}, ALICE)["motion"]["state"], "idle")
        self.assertEqual(len(self.driver.jobs), 1)
        self.command("/api/move", **{"from": "d2", "to": "d4"})
        self.settled()
        self.assertEqual(len(self.driver.jobs), 3)

    def test_resolving_without_a_fault_cannot_drop_queued_moves(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.service.mutate("/api/motion-resolve", {}, ALICE)
        self.driver.gate.set()
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2)

    def test_restart_during_a_job_never_assumes_it_finished(self):
        self.command("/api/new")
        self.driver.gate = threading.Event()
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        deadline = time.monotonic() + 5
        while self.service.db.execute("SELECT COUNT(*) FROM motion_jobs WHERE status='running'").fetchone()[0] == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.service.close()
        self.service = self.start()
        motion = self.service.status(ALICE)["motion"]
        self.assertEqual(motion["state"], "fault")
        self.assertIn("restarted", motion["error"])

    def test_takeback_and_new_game_move_the_pieces_back(self):
        self.command("/api/new")
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.settled()
        self.command("/api/undo")
        self.settled()
        self.assertEqual([job["label"] for job in self.driver.jobs[2:]], ["undo " + reply["computer"]["uci"], "undo e2e4"])
        self.driver.jobs.clear()
        self.command("/api/new")
        self.settled()
        self.assertEqual(self.driver.jobs, [])
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.settled()
        self.driver.jobs.clear()
        self.command("/api/new")
        self.settled()
        self.assertEqual([job["label"] for job in self.driver.jobs], ["reset"])


class FakeCamera:
    """Sees whatever a test tells it to, or the true position if `follow` is set."""

    def __init__(self):
        self.next, self.follow, self.fail, self.photos = None, None, False, 0

    def observe(self):
        self.photos += 1
        if self.fail:
            raise OSError("camera unplugged")
        return self.next if self.next else BoardObservation(frozenset(self.follow()))


class BoardSyncTests(QueueTestCase):
    """The pi-only flow: a person moves the pieces and the camera checks the result."""

    def start(self, driver=None):
        self.camera = getattr(self, "camera", None) or FakeCamera()
        return GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60,
                           motion_driver=driver or ManualDriver(), camera=self.camera)

    def confirm(self, **body):
        return self.service.mutate("/api/board-confirm", body, ALICE)

    def waiting_on(self, label):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            motion = self.service.status(ALICE)["motion"]
            if motion["state"] == "waiting" and motion["current"] == label:
                return motion
            time.sleep(0.005)
        self.fail(f"never waited on {label}: {motion}")

    def after(self, *moves):
        board = chess.Board()
        for uci in moves:
            board.push_uci(uci)
        return occupied_squares(board)

    def test_queue_waits_for_a_person_and_tells_them_what_to_do(self):
        self.command("/api/new")
        self.camera.follow = lambda: self.after("e2e4")
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        motion = self.waiting_on("e2e4")
        self.assertEqual(motion["instructions"], ["Move the white pawn from e2 to e4."])
        self.assertEqual(motion["pending"], 1)
        self.assertEqual(self.camera.photos, 0)
        self.confirm()
        reply_uci = reply["computer"]["uci"]
        motion = self.waiting_on(reply_uci)
        self.assertEqual(self.camera.photos, 1)
        self.camera.follow = lambda: self.after("e2e4", reply_uci)
        self.confirm()
        self.settled()

    def test_wrong_board_keeps_waiting_and_says_what_is_wrong(self):
        self.command("/api/new")
        self.camera.follow = lambda: self.after()
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.waiting_on("e2e4")
        self.confirm()
        deadline = time.monotonic() + 5
        while not self.service.status(ALICE)["motion"]["mismatch"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        motion = self.service.status(ALICE)["motion"]
        self.assertEqual(motion["state"], "waiting")
        self.assertEqual(motion["mismatch"], {"missing": ["e4"], "extra": ["e2"], "uncertain": []})
        self.camera.follow = lambda: self.after("e2e4")
        self.confirm()
        self.waiting_on(self.service.record["uciMoves"][1])

    def test_uncertain_photo_and_broken_camera_ask_again_instead_of_failing(self):
        self.command("/api/new")
        self.camera.next = BoardObservation(frozenset(), ("d4",))
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.waiting_on("e2e4")
        self.confirm()
        deadline = time.monotonic() + 5
        while not self.service.status(ALICE)["motion"]["mismatch"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.assertEqual(self.service.status(ALICE)["motion"]["mismatch"]["uncertain"], ["d4"])
        self.camera.fail = True
        before = self.camera.photos
        with self.assertLogs(level="ERROR"):
            self.confirm()
            deadline = time.monotonic() + 5
            while self.camera.photos == before or not self.service.status(ALICE)["motion"]["mismatch"].get("error"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
        motion = self.service.status(ALICE)["motion"]
        self.assertEqual(motion["state"], "waiting")
        self.assertNotIn("unplugged", motion["mismatch"]["error"])

    def test_override_accepts_the_board_without_the_camera(self):
        self.command("/api/new")
        self.camera.next = BoardObservation(frozenset(), ("d4",))
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.waiting_on("e2e4")
        self.confirm(override=True)
        self.waiting_on(reply["computer"]["uci"])
        self.assertEqual(self.camera.photos, 0)

    def test_without_a_camera_the_persons_word_is_enough(self):
        self.service.close()
        self.camera = None
        self.service = GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60, motion_driver=ManualDriver())
        self.command("/api/new")
        reply = self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.assertFalse(reply["motion"]["camera"])
        self.waiting_on("e2e4")
        self.confirm()
        self.waiting_on(reply["computer"]["uci"])
        self.confirm()
        self.settled()

    def test_restart_while_waiting_asks_the_person_again(self):
        self.command("/api/new")
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.waiting_on("e2e4")
        self.service.close()
        self.service = self.start()
        self.assertEqual(self.waiting_on("e2e4")["instructions"], ["Move the white pawn from e2 to e4."])

    def test_motor_job_checked_by_camera_stops_on_a_wrong_board(self):
        self.service.close()
        self.service = self.start(self.driver)
        self.command("/api/new")
        self.camera.follow = lambda: self.after()
        with self.assertLogs(level="WARNING"):
            self.command("/api/move", **{"from": "e2", "to": "e4"})
            motion = self.settled("fault")
        self.assertEqual(motion["current"], "e2e4")
        self.assertIn("camera", motion["error"])

    def test_motor_job_checked_by_camera_continues_on_the_right_board(self):
        self.service.close()
        self.service = self.start(self.driver)
        self.command("/api/new")
        self.camera.follow = lambda: self.driver.jobs[-1]["expect"]
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.settled()
        self.assertEqual(len(self.driver.jobs), 2)
        self.assertEqual(self.camera.photos, 2)

    def scan(self):
        return self.service.mutate("/api/board-scan", {}, ALICE)

    def test_scan_finds_the_move_and_commits_nothing(self):
        self.command("/api/new")
        self.camera.follow = lambda: self.after("g1f3")
        result = self.scan()
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidates"][0]["uci"], "g1f3")
        self.assertEqual(result["candidates"][0]["san"], "Nf3")
        self.assertEqual(self.service.status(ALICE)["uciMoves"], [])

    def test_a_move_read_off_the_board_only_queues_the_reply(self):
        self.command("/api/new")
        reply = self.command("/api/move", fromBoard=True, **{"from": "g1", "to": "f3"})
        self.assertEqual(reply["motion"]["pending"] + (reply["motion"]["state"] == "waiting"), 1)
        motion = self.waiting_on(reply["computer"]["uci"])
        self.assertEqual(motion["pending"], 0)

    def test_scan_reports_nothing_new_and_impossible_boards(self):
        self.command("/api/new")
        self.camera.follow = lambda: self.after()
        self.assertEqual(self.scan()["status"], "unchanged")
        self.camera.follow = lambda: self.after() - {"a1"}
        result = self.scan()
        self.assertEqual((result["status"], result["missing"]), ("no_match", ["a1"]))
        self.camera.next = BoardObservation(frozenset(), ("a1",))
        self.assertEqual(self.scan()["status"], "uncertain")

    def test_scan_refuses_while_the_board_is_busy_or_unavailable(self):
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "not_your_turn")
        self.command("/api/new")
        self.command("/api/move", **{"from": "e2", "to": "e4"})
        self.waiting_on("e2e4")
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "board_moving")
        self.service.close()
        self.service = GameService(self.path, lambda: self.engine, lambda: 1000, lease_seconds=60)
        with self.assertRaises(ApiError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "no_camera")


if __name__ == "__main__":
    unittest.main()
