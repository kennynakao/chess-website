"""Comparing photos with the game, and the adapter for the pi-only branch's vision code."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import chess

from backend.board_camera import BoardObservation, CameraError, VisionCamera, camera_from_env, compare, find_vision_dir, occupied_squares, scan_result
from backend.board_motion import ManualDriver, SimulatedDriver, CommandDriver, describe_job, driver_from_env, plan_transition
from backend.gantry import ArduinoGantry

# The same names and shapes as board_vision.py and pi_camera.py on the pi-only branch.
FAKE_BOARD_VISION = """
from dataclasses import dataclass
@dataclass(frozen=True)
class Observation:
    occupied: frozenset
    uncertain: tuple
    scores: dict
def load_calibration(directory):
    if not (directory / "calibration.json").exists():
        raise ValueError("Could not load calibration from " + str(directory))
    return ("calibration", "reference")
def read_image(path):
    return "image " + path.name
def observe(reference, photo, calibration, corners=None):
    assert (reference, photo, corners) == ("reference", "image latest.jpg", None)
    return Observation(frozenset({"a1", "h8"}), ("c3",), {}), "aligned"
"""
FAKE_PI_CAMERA = """
class Camera:
    def __init__(self, kind="pi", device=0):
        self.kind, self.device = kind, device
    def capture(self, path):
        path.write_text(f"{self.kind} {self.device}")
        return path
"""


class MotorProgramTests(unittest.TestCase):
    def test_a_hung_program_and_anything_it_started_are_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "child"
            script = Path(temp) / "motor.py"
            script.write_text(textwrap.dedent(f"""
                import subprocess, sys, time
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                open({str(pid_file)!r}, "w").write(str(child.pid))
                time.sleep(60)
            """))
            driver = CommandDriver(f'"{sys.executable}" "{script}"', timeout=1)
            with self.assertRaises(subprocess.TimeoutExpired):
                driver.execute({"id": 1, "label": "e2e4", "steps": []})
            child = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            self.fail("the program's child kept running")

    def test_a_missing_program_is_an_error_not_a_hang(self):
        with self.assertRaises(OSError):
            CommandDriver("/nonexistent/motor").execute({"id": 1, "label": "x", "steps": []})

    def test_the_time_limit_grows_with_the_job(self):
        seen = {}
        class Done:
            pid, returncode = 1, 0
            def communicate(self, data, timeout):
                seen["timeout"] = timeout
        with mock.patch("backend.board_motion.subprocess.Popen", return_value=Done()):
            CommandDriver("motor").execute({"id": 1, "label": "x", "steps": [{}] * 10})
        self.assertEqual(seen["timeout"], 360)


class ComparisonTests(unittest.TestCase):
    def test_matching_board_has_no_problem(self):
        self.assertIsNone(compare(["a1", "b2"], BoardObservation(frozenset({"a1", "b2"}))))

    def test_differences_name_the_squares(self):
        self.assertEqual(compare(["a1", "b2"], BoardObservation(frozenset({"a1", "c3"}))),
                         {"missing": ["b2"], "extra": ["c3"], "uncertain": []})

    def test_an_uncertain_photo_is_never_trusted(self):
        problem = compare(["a1"], BoardObservation(frozenset({"a1"}), ("h8",)))
        self.assertEqual(problem["uncertain"], ["h8"])

    def test_capture_scan_finds_the_capturing_move(self):
        board = chess.Board()
        for uci in ["e2e4", "d7d5"]:
            board.push_uci(uci)
        after = occupied_squares(board) - {"e4"}
        result = scan_result(board, BoardObservation(after))
        self.assertEqual((result["status"], [c["uci"] for c in result["candidates"]]), ("matched", ["e4d5"]))

    def test_promotions_look_identical_so_they_are_ambiguous(self):
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        result = scan_result(board, BoardObservation(frozenset({"a8", "h8", "h1"})))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({c["promotion"] for c in result["candidates"]}, {"q", "r", "b", "n"})

    def test_expected_squares_are_planned_with_each_job(self):
        forward = plan_transition([], ["e2e4", "d7d5", "e4d5"])
        board = chess.Board()
        for uci, job in zip(["e2e4", "d7d5", "e4d5"], forward):
            board.push_uci(uci)
            self.assertEqual(set(job["expect"]), occupied_squares(board), uci)
        undo = plan_transition(["e2e4", "d7d5", "e4d5"], ["e2e4"])
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(set(undo[-1]["expect"]), occupied_squares(board))
        board.push_uci("d7d5")
        self.assertEqual(set(undo[0]["expect"]), occupied_squares(board))
        self.assertEqual(set(plan_transition(["e2e4"], [], new_game=True)[0]["expect"]), occupied_squares(chess.Board()))

    def test_jobs_say_what_is_on_the_board_before_them(self):
        """The gantry program plans its own paths from "before" and "pieces", with each piece's real width."""
        forward = plan_transition([], ["e2e4", "d7d5", "e4d5"])
        self.assertEqual(len(forward[0]["before"]), 32)
        self.assertEqual(forward[0]["pieces"]["e2"], "P")
        self.assertNotIn("after_pieces", forward[0])
        self.assertIn("e4", forward[1]["before"])
        self.assertNotIn("e2", forward[1]["before"])
        self.assertEqual(forward[2]["pieces"]["d5"], "p", "the pawn about to be taken")
        undo = plan_transition(["e2e4", "d7d5", "e4d5"], ["e2e4"])
        self.assertEqual((undo[0]["pieces"]["d5"], "e4" in undo[0]["pieces"]), ("P", False), "undoing the capture starts from the board after it")
        self.assertEqual(sum(1 for symbol in undo[0]["pieces"].values() if symbol == "p"), 7, "the taken pawn is off the board, not docked beside it")
        self.assertTrue(all(":" not in name for name in undo[0]["pieces"]))
        reset = plan_transition(["e2e4"], [], new_game=True)
        self.assertIn("e4", reset[0]["before"])


class InstructionTests(unittest.TestCase):
    def describe(self, *history):
        return describe_job(plan_transition(list(history[:-1]), list(history))[-1])

    def test_plain_move(self):
        self.assertEqual(self.describe("g1f3"), ["Move the white knight from g1 to f3."])

    def test_capture_says_to_remove_the_piece_first(self):
        self.assertEqual(self.describe("e2e4", "d7d5", "e4d5"),
                         ["Take the black pawn off d5, then press Check.", "Move the white pawn from e4 to d5."])

    def test_castling_moves_both_pieces(self):
        self.assertEqual(self.describe("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "e1g1"),
                         ["Move the white king from e1 to g1.", "Move the white rook from h1 to f1."])

    def test_takeback_puts_a_captured_piece_back(self):
        back = plan_transition(["e2e4", "d7d5", "e4d5"], ["e2e4", "d7d5"])
        self.assertEqual(describe_job(back[0]), ["Move the white pawn from d5 to e4.", "Put the black pawn back on d5."])

    def test_reset_is_one_instruction(self):
        self.assertEqual(describe_job(plan_transition(["e2e4"], [], new_game=True)[0]),
                         ["Set up the standard starting position."])


class EnvironmentTests(unittest.TestCase):
    def test_driver_choice(self):
        with mock.patch.dict(os.environ, {"CHESS_MOTION_COMMAND": "", "CHESS_BOARD_MODE": ""}):
            self.assertIsInstance(driver_from_env(), SimulatedDriver)
        with mock.patch.dict(os.environ, {"CHESS_MOTION_COMMAND": "", "CHESS_BOARD_MODE": "manual"}):
            self.assertIsInstance(driver_from_env(), ManualDriver)
        with mock.patch.dict(os.environ, {"CHESS_MOTION_COMMAND": "/bin/true", "CHESS_BOARD_MODE": "manual"}):
            self.assertIsInstance(driver_from_env(), CommandDriver)
        with mock.patch.dict(os.environ, {"CHESS_MOTION_COMMAND": "", "CHESS_GANTRY": "arduino", "CHESS_BOARD_MODE": ""}):
            self.assertIsInstance(driver_from_env(), ArduinoGantry)
        with mock.patch.dict(os.environ, {"CHESS_MOTION_COMMAND": "", "CHESS_GANTRY": "teleport", "CHESS_BOARD_MODE": ""}), self.assertLogs(level="WARNING"):
            self.assertIsInstance(driver_from_env(), SimulatedDriver)

    def test_no_camera_unless_the_code_is_there(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "CHESS_VISION_DIR": ""}):
            self.assertIsNone(camera_from_env())

    def test_broken_camera_setup_keeps_the_game_running(self):
        with mock.patch.dict(os.environ, {"CHESS_VISION_DIR": "/nonexistent/vision"}), self.assertLogs(level="ERROR"):
            self.assertIsNone(camera_from_env())


class VisionCameraTests(unittest.TestCase):
    """Runs the real bridge in a real subprocess, against stand-ins for the branch's modules."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "board_vision.py").write_text(textwrap.dedent(FAKE_BOARD_VISION))
        (self.root / "pi_camera.py").write_text(textwrap.dedent(FAKE_PI_CAMERA))
        (self.root / "calibration").mkdir()
        (self.root / "calibration" / "calibration.json").write_text("{}")

    def tearDown(self):
        self.temp.cleanup()

    def camera(self, **options):
        return VisionCamera(self.root, python=sys.executable, **options)

    def test_returns_what_the_branch_code_sees_without_piling_up_photos(self):
        camera = self.camera(kind="usb", device=2)
        self.assertEqual(camera.observe(), BoardObservation(frozenset({"a1", "h8"}), ("c3",)))
        camera.observe()
        self.assertEqual(sorted(p.name for p in (self.root / "calibration").iterdir()), ["calibration.json", "latest.jpg"])
        self.assertEqual((self.root / "calibration" / "latest.jpg").read_text(), "usb 2")

    def test_missing_calibration_is_reported_when_a_photo_is_taken(self):
        (self.root / "calibration" / "calibration.json").unlink()
        camera = self.camera()
        with self.assertRaisesRegex(CameraError, "Could not load calibration"):
            camera.observe()

    def test_noise_before_the_answer_is_ignored_but_a_bad_answer_is_an_error(self):
        vision = self.root / "board_vision.py"
        vision.write_text(vision.read_text().replace("def read_image", "print('loading')\ndef read_image"))
        self.assertEqual(self.camera().observe().occupied, frozenset({"a1", "h8"}))
        (self.root / "pi_camera.py").write_text(textwrap.dedent(FAKE_PI_CAMERA))
        vision.write_text(vision.read_text().replace('frozenset({"a1", "h8"})', "None"))
        with self.assertRaises(CameraError):
            self.camera().observe()

    def test_uses_the_checkouts_own_python_when_it_has_one(self):
        self.assertEqual(VisionCamera(self.root).python, sys.executable)
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("")
        self.assertEqual(VisionCamera(self.root).python, str(python))

    def test_a_program_that_cannot_start_is_reported(self):
        with self.assertRaises(CameraError):
            VisionCamera(self.root, python=self.root / "no-such-python").observe()

    def test_the_touch_screens_checkout_is_preferred_and_only_read(self):
        home = self.root / "home"
        older, newer = home / "Self-Moving-Chess-Board-Code", home / "chess-board-pi-only"
        for folder in (older, newer):
            folder.mkdir(parents=True)
            (folder / "board_vision.py").write_text("")
        with mock.patch.dict(os.environ, {"HOME": str(home), "CHESS_VISION_DIR": ""}):
            self.assertEqual(find_vision_dir(), newer)

    def test_the_camera_switches_on_only_when_each_photo_could_work(self):
        home = self.root / "home"
        checkout = home / "Self-Moving-Chess-Board-Code"
        checkout.mkdir(parents=True)
        settings = {"HOME": str(home), "CHESS_VISION_DIR": "", "CHESS_VISION_PYTHON": "", "CHESS_CALIBRATION_DIR": ""}
        with mock.patch.dict(os.environ, settings):
            self.assertIsNone(find_vision_dir())
            self.assertIsNone(camera_from_env())
            # A fresh clone, like the Pi today: the code is there, nothing has been set up.
            (checkout / "board_vision.py").write_text("")
            self.assertEqual(find_vision_dir(), checkout)
            with self.assertLogs(level="WARNING") as logged:
                self.assertIsNone(camera_from_env())
            self.assertIn(".venv", logged.output[0])
            python = checkout / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            with self.assertLogs(level="WARNING") as logged:
                self.assertIsNone(camera_from_env())
            self.assertIn("pi_game.py setup", logged.output[0])
            (checkout / "calibration").mkdir()
            (checkout / "calibration" / "calibration.json").write_text("{}")
            camera = camera_from_env()
            self.assertEqual((camera.vision_dir, camera.python), (checkout, str(python)))


if __name__ == "__main__":
    unittest.main()
