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

Three ways to perform a job, chosen by driver_from_env():

    CHESS_MOTION_COMMAND=...   a motor program moves the pieces
    CHESS_BOARD_MODE=manual    a person moves them, as in the pi-only branch of
                               Self-Moving-Chess-Board-Code, and confirms on the website
    neither                    nothing moves; the jobs are only logged
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess

import chess

SLOTS_PER_COLOR = 16
SLOTS_PER_COLUMN = 8
# A full reset can carry every piece on the board, so this is deliberately generous.
COMMAND_TIMEOUT_SECONDS = 900


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
        steps = _plan_ply(layout, board, move)
        board.push(move)
        plies.append({"label": uci, "steps": steps, "expect": _on_board(layout)})
    return layout, plies


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
        steps = _plan_reset(layout)
        return [{"label": "reset", "steps": steps, "expect": _on_board(Layout())}] if steps else []
    if after[:len(before)] == before:
        return _replay(after)[1][len(before):]
    if before[:len(after)] == after:
        start = _on_board(Layout())
        return [{"label": "undo " + plies[i]["label"], "steps": _reversed_steps(plies[i]["steps"]),
                 "expect": plies[i - 1]["expect"] if i else start}
                for i in range(len(before) - 1, len(after) - 1, -1)]
    raise ValueError("These game histories are not connected by moves or takebacks.")


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
    """The default when nothing is connected: records the signals and moves nothing."""
    name = "simulated"
    moves_pieces, needs_person = False, False

    def execute(self, job: dict) -> None:
        logging.info("MOTION simulated job=%s label=%s steps=%s", job["id"], job["label"], json.dumps(job["steps"]))


class CommandDriver:
    """Runs the motor program once per job, with the job as JSON on standard input.

    The program returns when the pieces have finished moving: exit status 0 means
    done, anything else stops the queue until someone checks the board.
    """
    name = "command"
    moves_pieces, needs_person = True, False

    def __init__(self, command: str, timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        self.args, self.timeout = shlex.split(command), timeout

    def execute(self, job: dict) -> None:
        # Its output goes straight to the service log, which is where to debug the motors.
        subprocess.run(self.args, input=json.dumps(job), text=True, timeout=self.timeout, check=True)


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
    return ManualDriver() if os.environ.get("CHESS_BOARD_MODE", "").strip() == "manual" else SimulatedDriver()
