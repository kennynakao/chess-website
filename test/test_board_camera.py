"""Comparing photos with the game, and the adapter for the pi-only branch's vision code."""
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import chess

from backend.board_camera import BoardObservation, VisionCamera, camera_from_env, compare, occupied_squares, scan_result
from backend.board_motion import ManualDriver, SimulatedDriver, CommandDriver, describe_job, driver_from_env, plan_transition

# The same names and shapes as board_vision.py and pi_camera.py on the pi-only branch.
FAKE_BOARD_VISION = """
from dataclasses import dataclass
@dataclass(frozen=True)
class Observation:
    occupied: frozenset
    uncertain: tuple
    scores: dict
def load_calibration(directory):
    return ("calibration from " + str(directory.name), "reference")
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
        path.write_text("photo")
        return path
"""


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


class InstructionTests(unittest.TestCase):
    def describe(self, *history):
        return describe_job(plan_transition(list(history[:-1]), list(history))[-1])

    def test_plain_move(self):
        self.assertEqual(self.describe("g1f3"), ["Move the white knight from g1 to f3."])

    def test_capture_says_to_remove_the_piece_first(self):
        self.assertEqual(self.describe("e2e4", "d7d5", "e4d5"),
                         ["Remove the black pawn from d5.", "Move the white pawn from e4 to d5."])

    def test_castling_moves_both_pieces(self):
        self.assertEqual(self.describe("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "e1g1"),
                         ["Move the white king from e1 to g1.", "Move the white rook from h1 to f1."])

    def test_takeback_puts_a_captured_piece_back(self):
        back = plan_transition(["e2e4", "d7d5", "e4d5"], ["e2e4", "d7d5"])
        self.assertEqual(describe_job(back[0]), ["Move the white pawn from d5 to e4.", "Put the black pawn on d5."])

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

    def test_no_camera_unless_configured(self):
        with mock.patch.dict(os.environ, {"CHESS_VISION_DIR": ""}):
            self.assertIsNone(camera_from_env())

    def test_broken_camera_setup_keeps_the_game_running(self):
        with mock.patch.dict(os.environ, {"CHESS_VISION_DIR": "/nonexistent/vision"}), self.assertLogs(level="ERROR"):
            self.assertIsNone(camera_from_env())


class VisionCameraAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "board_vision.py").write_text(textwrap.dedent(FAKE_BOARD_VISION))
        (self.root / "pi_camera.py").write_text(textwrap.dedent(FAKE_PI_CAMERA))
        (self.root / "calibration").mkdir()
        self.path_before = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.path_before
        for name in ("board_vision", "pi_camera"):
            sys.modules.pop(name, None)
        self.temp.cleanup()

    def test_loads_the_branch_modules_and_returns_their_observation(self):
        camera = VisionCamera(self.root, kind="usb", device=2)
        self.assertEqual((camera.camera.kind, camera.camera.device), ("usb", 2))
        self.assertEqual(camera.calibration, "calibration from calibration")
        seen = camera.observe()
        self.assertEqual(seen, BoardObservation(frozenset({"a1", "h8"}), ("c3",)))
        self.assertTrue((self.root / "calibration" / "latest.jpg").exists())
        camera.observe()
        self.assertEqual([p.name for p in (self.root / "calibration").iterdir()], ["latest.jpg"])


if __name__ == "__main__":
    unittest.main()
