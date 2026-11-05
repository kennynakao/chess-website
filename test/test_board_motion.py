"""The physical steps planned for every kind of chess move, and the drivers that run them."""
import json
import math
import random
import subprocess
import sys
import unittest

import chess

from backend.board_motion import CommandDriver, Layout, _plan_ply, _replay, _xy, plan_transition


def carries(job):
    return [step for step in job["steps"] if step["op"] == "carry"]


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

    def test_capture_parks_the_victim_before_moving_in(self):
        park, take = carries(self.last_job("e2e4", "d7d5", "e4d5"))
        self.assertEqual((park["piece"], park["from"], park["to"]), ("p", "d5", "b:0"))
        self.assertEqual((take["piece"], take["from"], take["to"]), ("P", "e4", "d5"))

    def test_en_passant_parks_the_pawn_that_is_not_on_the_target_square(self):
        park, take = carries(self.last_job("e2e4", "a7a6", "e4e5", "d7d5", "e5d6"))
        self.assertEqual((park["from"], park["to"]), ("d5", "b:0"))
        self.assertEqual((take["from"], take["to"]), ("e5", "d6"))

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

    def test_promotion_without_a_spare_leaves_the_pawn_standing_in(self):
        line = ["a2a4", "b7b5", "a4b5", "a7a6", "b5a6", "c8b7", "a6b7", "b8c6", "b7a8q"]
        board = chess.Board()
        for uci in line:
            board.push_uci(uci)
        job = plan_transition(line[:-1], line)[-1]
        self.assertEqual([step["op"] for step in job["steps"]], ["carry", "carry", "note"])
        self.assertEqual((job["steps"][0]["from"], job["steps"][0]["to"]), ("a8", "b:3"))
        self.assertIn("a8 now counts as a queen", job["steps"][2]["message"])
        layout, _ = _replay(line)
        self.assertEqual(layout.pieces["a8"], "P")

    def test_promotion_uses_a_captured_piece_of_that_type_when_there_is_one(self):
        layout = Layout()
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        layout.pieces = {"a7": "P", "h8": "k", "h1": "K", "w:0": "Q"}
        park, bring = _plan_ply(layout, board, chess.Move.from_uci("a7a8q"))
        self.assertEqual((park["piece"], park["from"], park["to"]), ("P", "a7", "w:1"))
        self.assertEqual((bring["piece"], bring["from"], bring["to"]), ("Q", "w:0", "a8"))

    def test_takeback_retraces_each_step_in_reverse(self):
        history = ["e2e4", "d7d5", "e4d5"]
        forward = plan_transition(history[:-1], history)[-1]
        back = plan_transition(history, history[:-1])[-1]
        self.assertEqual(back["label"], "undo e4d5")
        self.assertEqual([(s["from"], s["to"]) for s in carries(back)], [("d5", "e4"), ("b:0", "d5")])
        self.assertEqual(carries(back)[0]["path"], carries(forward)[1]["path"][::-1])

    def test_new_game_on_an_untouched_board_needs_no_job(self):
        self.assertEqual(plan_transition([], [], new_game=True), [])

    def test_unconnected_histories_are_refused(self):
        with self.assertRaises(ValueError):
            plan_transition(["e2e4"], ["d2d4"])

    def perform(self, pieces, steps):
        """Act out carries on a copy of the board, failing if any path touches another piece."""
        pieces = dict(pieces)
        for step in steps:
            if step["op"] != "carry":
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
                self.assertEqual(self.perform(before, steps), layout.pieces, (seed, move.uci()))
                board.push(move)
                history.append(move.uci())
                played[len(history)] = dict(layout.pieces)
                self.assertEqual({name for name in layout.pieces if ":" not in name},
                                 {chess.square_name(square) for square in board.piece_map()}, (seed, history))
            json.dumps(plan_transition(history[:-1], history))
            # Taking back the last two plies restores exactly what was there before them.
            if len(history) > 2:
                job_steps = [step for job in plan_transition(history, history[:-2]) for step in job["steps"]]
                self.assertEqual(self.perform(layout.pieces, job_steps), played[len(history) - 2], seed)
            jobs = plan_transition(history, [], new_game=True)
            self.assertLessEqual(len(jobs), 1)
            final = self.perform(layout.pieces, jobs[0]["steps"]) if jobs else layout.pieces
            self.assertEqual(final, Layout().pieces, seed)


class CommandDriverTests(unittest.TestCase):
    def test_program_receives_the_job_as_json_on_standard_input(self):
        script = "import json,sys;job=json.load(sys.stdin);sys.exit(0 if job['label']=='e2e4' else 3)"
        driver = CommandDriver(f'"{sys.executable}" -c "{script}"')
        driver.execute({"id": 1, "label": "e2e4", "steps": []})
        with self.assertRaises(subprocess.CalledProcessError):
            driver.execute({"id": 2, "label": "other", "steps": []})


if __name__ == "__main__":
    unittest.main()
