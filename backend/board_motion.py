"""Turn committed chess moves into the physical steps the board must perform.

Nothing here knows about motors. A job is plain JSON, handed to a driver:

    {"id": 7, "label": "e7e5", "expect": ["a1", "a2", ...], "steps": [
        {"op": "carry", "piece": "p", "from": "e7", "to": "e5", "path": [[4, 6], [4, 4]]}]}

"expect" lists every board square that should be occupied once the job is done, which
is what a camera can check.

Paths are in square units: a1 is (0, 0) and h8 is (7, 7). To perform a carry, go to
the first point, engage the piece, follow the points in order, then release it.
Whole numbers are square centers; x.5 and y.5 are the lanes between squares, where a
carried piece can pass other pieces. Captured pieces park off the board: White's at
x = -1 and -2, Black's at x = 8 and 9, with y from 0 to 7. Those slots are named
"w:0" to "w:15" and "b:0" to "b:15".

A step with op "note" moves nothing; it is a message for whoever is watching.

This is the gantry's slot in website board games, which board_pipeline.py runs in this
order: the player's move, gantry, camera, Stockfish's move, gantry, camera, back to the
player. The website's digital game and playing in person with pi_game.py or pi_touch.py
are separate and never come through here. Four ways to perform a job, chosen by
driver_from_env():

    CHESS_MOTION_COMMAND=...    a gantry program: it gets each job as JSON on standard input
    CHESS_GANTRY=arduino        the Pi-to-Arduino driver in gantry.py: NOT DONE YET, every job stops the game
    CHESS_BOARD_MODE=manual     a person moves the pieces and confirms on the website
    none of these               no gantry yet: the job is logged and the game carries on
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess

import chess

# Captured pieces dock in a lane beside the board: sketch 5 of the board design has twelve slots
# down each side ("w:0".."w:11" on White's side, "b:0".."b:11" on Black's). A thirteenth piece of
# one colour, which only happens after twelve captures, goes to a second lane (x = -2 / 9) that
# the Pi may or may not have configured.
SLOTS_PER_COLOR = 16
SLOTS_PER_COLUMN = 12
# A full reset can carry every piece on the board, so the limit grows with the job.
COMMAND_TIMEOUT_SECONDS = 60
COMMAND_TIMEOUT_PER_STEP_SECONDS = 30


def _xy(name: str) -> tuple[float, float]:
    """Center of a board square ("e4") or a parking slot ("w:3")."""
    if ":" in name:
        color, index = name[0], int(name[2:])
        column = index // SLOTS_PER_COLUMN
        x = -1 - column if color == "w" else 8 + column
        return float(x), float(index % SLOTS_PER_COLUMN)
    return float(ord(name[0]) - ord("a")), float(int(name[1]) - 1)


def _corner(x: float, y: float, toward_x: float, toward_y: float) -> tuple[float, float]:
    """The lane crossing beside (x, y) that is nearest the destination, staying on the board if possible."""
    dx = 0.5 if toward_x > x or (toward_x == x and x < 7) else -0.5
    dy = 0.5 if toward_y > y or (toward_y == y and y < 7) else -0.5
    return x + dx, y + dy


class Layout:
    """Where every physical piece is, keyed by square or parking slot."""

    def __init__(self) -> None:
        self.pieces = {chess.square_name(square): piece.symbol()
                       for square, piece in chess.Board().piece_map().items()}

    def _clear_line(self, source: str, target: str) -> bool:
        if ":" in source or ":" in target:
            return False
        (ax, ay), (bx, by) = _xy(source), _xy(target)
        dx, dy = bx - ax, by - ay
        if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
            return False
        steps = int(max(abs(dx), abs(dy)))
        return all(f"{chess.FILE_NAMES[int(ax + dx * i / steps)]}{int(ay + dy * i / steps) + 1}" not in self.pieces
                   for i in range(1, steps))

    def _route(self, source: str, target: str) -> list[list[float]]:
        a, b = _xy(source), _xy(target)
        if self._clear_line(source, target):
            return [list(a), list(b)]
        # Off center, along the lanes, and into the target from its nearest corner.
        near_a, near_b = _corner(*a, *b), _corner(*b, *a)
        points = [a, near_a, (near_b[0], near_a[1]), near_b, b]
        return [list(point) for i, point in enumerate(points) if i == 0 or point != points[i - 1]]

    def carry(self, source: str, target: str) -> dict:
        piece = self.pieces.pop(source)
        path = self._route(source, target)
        self.pieces[target] = piece
        return {"op": "carry", "piece": piece, "from": source, "to": target, "path": path}

    def park(self, square: str) -> dict:
        color = "w" if self.pieces[square].isupper() else "b"
        slot = next(f"{color}:{i}" for i in range(SLOTS_PER_COLOR) if f"{color}:{i}" not in self.pieces)
        return self.carry(square, slot)

    def parked(self, symbol: str) -> str | None:
        return next((name for name, piece in sorted(self.pieces.items()) if piece == symbol and ":" in name), None)


def _plan_ply(layout: Layout, board: chess.Board, move: chess.Move) -> list[dict]:
    source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
    steps = []
    if board.is_en_passant(move):
        steps.append(layout.park(target[0] + source[1]))
    elif board.is_capture(move):
        steps.append(layout.park(target))

    if board.is_castling(move):
        rank = source[1]
        rook_from, rook_to = ("h", "f") if board.is_kingside_castling(move) else ("a", "d")
        steps.append(layout.carry(source, target))
        steps.append(layout.carry(rook_from + rank, rook_to + rank))
    elif move.promotion:
        name = chess.piece_name(move.promotion)
        wanted = chess.Piece(move.promotion, board.turn).symbol()
        spare = layout.parked(wanted)
        if spare:
            steps.append(layout.park(source))
            steps.append(layout.carry(spare, target))
        else:
            # No spare piece to swap in, so the pawn stays and stands in for it.
            steps.append(layout.carry(source, target))
            steps.append({"op": "note", "message": f"The pawn on {target} now counts as a {name}."})
    else:
        steps.append(layout.carry(source, target))
    return steps


def _on_board(layout: Layout) -> list[str]:
    return sorted(name for name in layout.pieces if ":" not in name)


def _replay(history: list[str]) -> tuple[Layout, list[dict]]:
    layout, board, plies = Layout(), chess.Board(), []
    for uci in history:
        move = chess.Move.from_uci(uci)
        before = dict(layout.pieces)
        steps = _plan_ply(layout, board, move)
        board.push(move)
        # "before" and "pieces" let the gantry program find its own paths with the real piece widths.
        plies.append({"label": uci, "steps": steps, "expect": _on_board(layout),
                      "before": sorted(before), "pieces": before, "after_pieces": dict(layout.pieces)})
    return layout, plies


def _job(ply: dict) -> dict:
    return {key: value for key, value in ply.items() if key != "after_pieces"}


def _reversed_steps(steps: list[dict]) -> list[dict]:
    return [{**step, "from": step["to"], "to": step["from"], "path": step["path"][::-1]}
            for step in reversed(steps) if step["op"] == "carry"]


def _plan_reset(layout: Layout) -> list[dict]:
    home = Layout().pieces
    steps = [layout.park(name) for name in sorted(layout.pieces)
             if ":" not in name and home.get(name) != layout.pieces[name]]
    for square, symbol in sorted(home.items()):
        if square not in layout.pieces:
            steps.append(layout.carry(layout.parked(symbol), square))
    return steps


def plan_transition(before: list[str], after: list[str], new_game: bool = False) -> list[dict]:
    """Jobs that take the physical board from game history `before` to `after`, in order."""
    layout, plies = _replay(before)
    if new_game:
        pieces = dict(layout.pieces)
        steps = _plan_reset(layout)
        return [{"label": "reset", "steps": steps, "expect": _on_board(Layout()), "before": sorted(pieces), "pieces": pieces}] if steps else []
    if after[:len(before)] == before:
        return [_job(ply) for ply in _replay(after)[1][len(before):]]
    if before[:len(after)] == after:
        start = _on_board(Layout())
        return [{"label": "undo " + plies[i]["label"], "steps": _reversed_steps(plies[i]["steps"]),
                 "expect": plies[i - 1]["expect"] if i else start,
                 "before": sorted(plies[i]["after_pieces"]), "pieces": plies[i]["after_pieces"]}
                for i in range(len(before) - 1, len(after) - 1, -1)]
    raise ValueError("These game histories are not connected by moves or takebacks.")


def starting_position_check() -> dict:
    """A job with nothing to move: someone sets up the board, and it is checked before play."""
    return {"label": "reset", "steps": [], "expect": _on_board(Layout())}


def describe_job(job: dict) -> list[str]:
    """What a person has to do to perform a job by hand."""
    if job["label"] == "reset":
        return ["Set up the standard starting position."]
    lines = []
    for step in job["steps"]:
        if step["op"] != "carry":
            lines.append(step["message"])
            continue
        symbol = step["piece"]
        piece = f"{'white' if symbol.isupper() else 'black'} {chess.piece_name(chess.PIECE_SYMBOLS.index(symbol.lower()))}"
        if ":" in step["to"]:
            lines.append(f"Remove the {piece} from {step['from']}.")
        elif ":" in step["from"]:
            lines.append(f"Put the {piece} on {step['to']}.")
        else:
            lines.append(f"Move the {piece} from {step['from']} to {step['to']}.")
    return lines


class SimulatedDriver:
    """The default until a gantry is connected: the pipeline logs each job and moves on."""
    name = "simulated"
    moves_pieces, needs_person = False, False

    def execute(self, job: dict) -> None:
        logging.info("PIPELINE %s gantry: not connected, nothing moved", job["label"])


class CommandDriver:
    """Runs the motor program once per job, with the job as JSON on standard input.

    The program returns when the pieces have finished moving: exit status 0 means
    done, anything else stops the queue until someone checks the board.
    """
    name = "command"
    moves_pieces, needs_person = True, False

    def __init__(self, command: str, timeout: float | None = None) -> None:
        self.args, self.timeout = shlex.split(command), timeout

    def execute(self, job: dict) -> None:
        timeout = self.timeout or COMMAND_TIMEOUT_SECONDS + COMMAND_TIMEOUT_PER_STEP_SECONDS * len(job["steps"])
        # Its output goes straight to the service log, which is where to debug the motors.
        # Its own process group, so that stopping it also stops anything it started: nothing
        # may keep driving motors while someone is fixing the pieces by hand.
        process = subprocess.Popen(self.args, stdin=subprocess.PIPE, text=True, start_new_session=True)
        try:
            process.communicate(json.dumps(job), timeout=timeout)
        except subprocess.TimeoutExpired:
            self._stop(process)
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, self.args)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # The group already ended, and its id may even belong to someone else by now.
        process.wait()


class ManualDriver:
    """No motors: the queue waits for a person to move the pieces and confirm on the website."""
    name = "manual"
    moves_pieces, needs_person = True, True

    def execute(self, job: dict) -> None:
        logging.info("MOTION waiting for a person job=%s: %s", job["id"], " ".join(describe_job(job)))


def driver_from_env():
    command = os.environ.get("CHESS_MOTION_COMMAND", "").strip()
    if command:
        return CommandDriver(command)
    gantry = os.environ.get("CHESS_GANTRY", "").strip().lower()
    if gantry == "arduino":
        from backend.gantry import arduino_from_env
        return arduino_from_env()
    if gantry:
        logging.warning("GANTRY CHESS_GANTRY=%r is not a known driver; nothing is connected", gantry)
    return ManualDriver() if os.environ.get("CHESS_BOARD_MODE", "").strip() == "manual" else SimulatedDriver()
