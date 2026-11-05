"""The physical steps planned for every kind of chess move, and the drivers that run them."""
import json
import math
import random
import re
import subprocess
import sys
import unittest

import chess

from backend.board_motion import CommandDriver, Layout, _plan_ply, _plan_reset, _replay, _xy, plan_transition

# What a note asks a person to put on the board, for acting a plan out.
PUT_BACK = re.compile(r"Put (?:the|a) (white|black) (\w+) (?:back )?on ([a-h][1-8])\.")


def carries(job):
    return [step for step in job["steps"] if step["op"] == "carry"]


def notes(job):
    return [step["message"] for step in job["steps"] if step["op"] == "note"]


def distance_to_segment(point, a, b):
    ax, ay, bx, by, px, py = *a, *b, *point
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0 if not length else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class PlanningTests(unittest.TestCase):
    def last_job(self, *history):
        return plan_transition(list(history[:-1]), list(history))[-1]

    def test_pawn_push_travels_straight(self):
        step, = carries(self.last_job("e2e4"))
        self.assertEqual((step["piece"], step["from"], step["to"]), ("P", "e2", "e4"))
        self.assertEqual(step["path"], [[4, 1], [4, 3]])

    def test_knight_goes_around_pieces_along_the_lanes(self):
        step, = carries(self.last_job("g1f3"))
        self.assertEqual(step["path"], [[6, 0], [5.5, 0.5], [5.5, 1.5], [5, 2]])

    def test_capture_asks_for_the_victim_to_be_taken_off_before_moving_in(self):
        job = self.last_job("e2e4", "d7d5", "e4d5")
        remove, take = job["steps"]
        self.assertEqual(remove, {"op": "remove", "piece": "p", "square": "d5"})
        self.assertEqual((take["op"], take["piece"], take["from"], take["to"]), ("carry", "P", "e4", "d5"))
        self.assertIn("d5", job["expect"])
        self.assertNotIn("e4", job["expect"])
        self.assertEqual(job["pieces"]["d5"], "p", "the gantry program is told what stands on d5 before the job")

    def test_en_passant_takes_off_the_pawn_that_is_not_on_the_target_square(self):
        remove, take = self.last_job("e2e4", "a7a6", "e4e5", "d7d5", "e5d6")["steps"]
        self.assertEqual(remove, {"op": "remove", "piece": "p", "square": "d5"})
        self.assertEqual((take["from"], take["to"]), ("e5", "d6"))

    def test_nothing_ever_goes_to_a_docking_slot(self):
        for job in plan_transition([], ["e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5a5", "c3b5", "a5b5"]):
            for step in carries(job):
                self.assertTrue(":" not in step["from"] and ":" not in step["to"], step)

    def test_castling_moves_both_pieces_and_the_rook_goes_around_the_king(self):
        history = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "e1g1"]
        king, rook = carries(self.last_job(*history))
        self.assertEqual((king["from"], king["to"]), ("e1", "g1"))
        self.assertEqual((rook["from"], rook["to"]), ("h1", "f1"))
        self.assertEqual(rook["path"], [[7, 0], [6.5, 0.5], [5.5, 0.5], [5, 0]])

    def test_queenside_castling_rook_travels_straight_when_clear(self):
        history = ["d2d4", "d7d5", "c1f4", "c8f5", "b1c3", "b8c6", "d1d2", "d8d7", "e1c1"]
        king, rook = carries(self.last_job(*history))
        self.assertEqual((king["from"], king["to"], rook["from"], rook["to"]), ("e1", "c1", "a1", "d1"))

    def test_promotion_carries_the_pawn_which_stands_in_for_the_new_piece(self):
        line = ["a2a4", "b7b5", "a4b5", "a7a6", "b5a6", "c8b7", "a6b7", "b8c6", "b7a8q"]
        job = plan_transition(line[:-1], line)[-1]
        self.assertEqual([step["op"] for step in job["steps"]], ["remove", "carry", "note"], "bxa8=Q takes the rook first")
        self.assertEqual(job["steps"][0], {"op": "remove", "piece": "r", "square": "a8"})
        self.assertEqual((job["steps"][1]["piece"], job["steps"][1]["from"], job["steps"][1]["to"]), ("P", "b7", "a8"))
        self.assertEqual(job["steps"][2]["message"], "The pawn on a8 now counts as a queen; swap it by hand if you have a spare piece.")
        layout, _ = _replay(line)
        self.assertEqual(layout.pieces["a8"], "P")
        quiet = _plan_ply(Layout(), chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1"), chess.Move.from_uci("a7a8n"))
        self.assertEqual([step["op"] for step in quiet], ["carry", "note"])
        self.assertIn("counts as a knight", quiet[1]["message"])

    def test_takeback_retraces_the_carry_and_asks_for_the_taken_piece(self):
        history = ["e2e4", "d7d5", "e4d5"]
        forward = plan_transition(history[:-1], history)[-1]
        back = plan_transition(history, history[:-1])[-1]
        self.assertEqual(back["label"], "undo e4d5")
        self.assertEqual([(s["op"], s.get("from"), s.get("to")) for s in back["steps"]], [("carry", "d5", "e4"), ("note", None, None)])
        self.assertEqual(notes(back), ["Put the black pawn back on d5."])
        self.assertEqual(carries(back)[0]["path"], carries(forward)[0]["path"][::-1])
        self.assertIn("d5", back["expect"], "the camera then checks that the pawn is back")
        self.assertEqual((back["pieces"]["d5"], "e4" in back["pieces"]), ("P", False), "the takeback starts from the board after the capture")

    def test_taking_back_en_passant_asks_for_the_passed_pawn(self):
        history = ["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"]
        back = plan_transition(history, history[:-1])[-1]
        self.assertEqual([(s["from"], s["to"]) for s in carries(back)], [("d6", "e5")])
        self.assertEqual(notes(back), ["Put the black pawn back on d5."])
        self.assertTrue({"d5", "e5"} <= set(back["expect"]) and "d6" not in back["expect"])

    def test_reset_carries_strays_home_and_asks_for_the_pieces_that_are_gone(self):
        job, = plan_transition(["e2e4", "d7d5", "e4d5", "d8d5"], [], new_game=True)
        self.assertEqual(job["label"], "reset")
        self.assertEqual([(s["piece"], s["from"], s["to"]) for s in carries(job)], [("q", "d5", "d8")])
        self.assertEqual(notes(job), ["Put a black pawn on d7.", "Put a white pawn on e2."])
        self.assertEqual(set(job["expect"]), set(Layout().pieces), "the camera checks the full starting position")
        self.assertEqual(job["pieces"]["d5"], "q")

    def test_reset_untangles_two_strays_on_each_others_home_squares(self):
        layout = Layout()
        layout.pieces["a1"], layout.pieces["b1"] = "N", "R"
        before = dict(layout.pieces)
        steps = _plan_reset(layout)
        self.assertEqual([(s["piece"], s["from"], s["to"]) for s in steps], [("R", "b1", "a3"), ("N", "a1", "b1"), ("R", "a3", "a1")])
        self.assertEqual(self.perform(before, steps), Layout().pieces)

    def test_new_game_on_an_untouched_board_needs_no_job(self):
        self.assertEqual(plan_transition([], [], new_game=True), [])

    def test_unconnected_histories_are_refused(self):
        with self.assertRaises(ValueError):
            plan_transition(["e2e4"], ["d2d4"])

    def perform(self, pieces, steps):
        """Act out a plan on a copy of the board: a person does the removes and what the notes ask, and no carry may touch another piece."""
        pieces = dict(pieces)
        for step in steps:
            if step["op"] == "remove":
                self.assertEqual(pieces.pop(step["square"]), step["piece"], step)
                continue
            if step["op"] == "note":
                asked = PUT_BACK.fullmatch(step["message"])
                if asked:
                    self.assertNotIn(asked.group(3), pieces, step)
                    pieces[asked.group(3)] = chess.Piece(chess.PIECE_NAMES.index(asked.group(2)), asked.group(1) == "white").symbol()
                continue
            self.assertEqual(pieces.pop(step["from"]), step["piece"], step)
            self.assertNotIn(step["to"], pieces, step)
            for a, b in zip(step["path"], step["path"][1:]):
                for name in pieces:
                    self.assertGreaterEqual(distance_to_segment(_xy(name), a, b), 0.5 - 1e-9, (step, name))
            self.assertEqual(step["path"][0], list(_xy(step["from"])))
            self.assertEqual(step["path"][-1], list(_xy(step["to"])))
            pieces[step["to"]] = step["piece"]
        return pieces

    def test_random_games_stay_in_sync_clear_of_other_pieces_and_reset_exactly(self):
        for seed in range(40):
            rng = random.Random(seed)
            board, layout, history, played = chess.Board(), Layout(), [], {}
            while not board.is_game_over() and len(history) < 200:
                move = rng.choice(list(board.legal_moves))
                before = dict(layout.pieces)
                steps = _plan_ply(layout, board, move)
                ops = [step["op"] for step in steps]
                if board.is_capture(move):
                    self.assertEqual(ops[:2], ["remove", "carry"], (seed, move.uci()))
                self.assertEqual(ops.count("remove"), int(board.is_capture(move)), (seed, move.uci()))
                self.assertEqual(self.perform(before, steps), layout.pieces, (seed, move.uci()))
                board.push(move)
                history.append(move.uci())
                played[len(history)] = dict(layout.pieces)
                self.assertEqual(set(layout.pieces), {chess.square_name(square) for square in board.piece_map()}, (seed, history))
            json.dumps(plan_transition(history[:-1], history))
            # Taking back the last two plies restores exactly what was there before them, once the person
            # has put back what the notes ask for (each job's camera check waits for that).
            if len(history) > 2:
                job_steps = [step for job in plan_transition(history, history[:-2]) for step in job["steps"]]
                self.assertEqual(self.perform(layout.pieces, job_steps), played[len(history) - 2], seed)
            jobs = plan_transition(history, [], new_game=True)
            self.assertLessEqual(len(jobs), 1)
            final = self.perform(layout.pieces, jobs[0]["steps"]) if jobs else layout.pieces
            self.assertEqual(final, Layout().pieces, seed)
            if jobs:
                self.assertEqual(set(jobs[0]["expect"]), set(Layout().pieces))
                self.assertEqual(len(notes(jobs[0])), 32 - len(layout.pieces), (seed, "one note per piece that is off the board"))


class CommandDriverTests(unittest.TestCase):
    def test_program_receives_the_job_as_json_on_standard_input(self):
        script = "import json,sys;job=json.load(sys.stdin);sys.exit(0 if job['label']=='e2e4' else 3)"
        driver = CommandDriver(f'"{sys.executable}" -c "{script}"')
        driver.execute({"id": 1, "label": "e2e4", "steps": []})
        with self.assertRaises(subprocess.CalledProcessError):
            driver.execute({"id": 2, "label": "other", "steps": []})


if __name__ == "__main__":
    unittest.main()
