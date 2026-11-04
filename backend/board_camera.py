"""See the physical board, so the game can check it against what should be there.

This is the camera's slot in website games. The vision code lives on the pi-only branch
of Self-Moving-Chess-Board-Code. If a checkout of it is at ~/chess-board-pi-only or
~/Self-Moving-Chess-Board-Code, the service uses it automatically, without changing it.
It runs under that checkout's own .venv, so the game service does not need OpenCV.

The camera only switches on once that .venv and a calibration from `pi_game.py setup`
both exist, because each photo depends on them. Until then the game still runs: a person
moves the pieces and their word is taken for it. Restart the service after calibrating.
These settings change the defaults:

    CHESS_VISION_DIR       the checkout, if it is somewhere else
    CHESS_VISION_PYTHON    its Python, if it is not at .venv/bin/python
    CHESS_CALIBRATION_DIR  what `pi_game.py setup` wrote (default: calibration/ in the checkout)
    CHESS_CAMERA           "pi" (default) or "usb"
    CHESS_CAMERA_DEVICE    camera index (default 0)

A camera only sees which squares are occupied, not which piece is on them. Moves that
leave the same squares occupied, such as the four promotions, therefore come back as
"ambiguous" and someone has to choose.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import chess

BRIDGE = Path(__file__).with_name("vision_bridge.py")
DEFAULT_VISION_DIRS = ("~/chess-board-pi-only", "~/Self-Moving-Chess-Board-Code")
# Under the website's 25 second limit, so a stuck camera is reported as a camera problem
# and not as an offline Pi. Loading OpenCV and taking the photo normally takes a few seconds.
PHOTO_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class BoardObservation:
    occupied: frozenset[str]
    uncertain: tuple[str, ...] = ()


def occupied_squares(board: chess.Board) -> frozenset[str]:
    return frozenset(chess.square_name(square) for square in board.piece_map())


def compare(expected, observation: BoardObservation) -> dict | None:
    """None when the photo matches; otherwise what is wrong with the board."""
    if observation.uncertain:
        return {"missing": [], "extra": [], "uncertain": sorted(observation.uncertain)}
    wanted = frozenset(expected)
    missing, extra = wanted - observation.occupied, observation.occupied - wanted
    if not missing and not extra:
        return None
    return {"missing": sorted(missing), "extra": sorted(extra), "uncertain": []}


def scan_result(board: chess.Board, observation: BoardObservation) -> dict:
    """Which legal moves the photo could be showing. Never changes the board."""
    result = {"status": "no_match", "candidates": [], "uncertain": sorted(observation.uncertain),
              "missing": [], "extra": []}
    if observation.uncertain:
        return {**result, "status": "uncertain"}
    now = occupied_squares(board)
    if observation.occupied == now:
        return {**result, "status": "unchanged"}
    for move in board.legal_moves:
        after = board.copy(stack=False)
        after.push(move)
        if occupied_squares(after) == observation.occupied:
            result["candidates"].append({
                "uci": move.uci(), "san": board.san(move), "from": chess.square_name(move.from_square),
                "to": chess.square_name(move.to_square),
                "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
            })
    if result["candidates"]:
        return {**result, "status": "matched" if len(result["candidates"]) == 1 else "ambiguous"}
    return {**result, "missing": sorted(now - observation.occupied), "extra": sorted(observation.occupied - now)}


class CameraError(RuntimeError):
    """The pi-only code could not produce a reading of the board."""


class VisionCamera:
    """Connector to the pi-only code. Each photo runs vision_bridge.py under that code's own Python."""
    name = "vision"

    def __init__(self, vision_dir: str | Path, calibration_dir: str | Path | None = None, kind: str = "pi",
                 device: int = 0, python: str | Path | None = None, timeout: float = PHOTO_TIMEOUT_SECONDS) -> None:
        self.vision_dir = Path(vision_dir).expanduser()
        if not (self.vision_dir / "board_vision.py").is_file():
            raise FileNotFoundError(f"The pi-only code was not found in {self.vision_dir}")
        self.calibration_dir = Path(calibration_dir).expanduser() if calibration_dir else self.vision_dir / "calibration"
        venv_python = self.vision_dir / ".venv" / "bin" / "python"
        self.python = str(python or (venv_python if venv_python.exists() else sys.executable))
        self.kind, self.device, self.timeout = kind, device, timeout

    def observe(self) -> BoardObservation:
        command = [self.python, str(BRIDGE), str(self.vision_dir), str(self.calibration_dir), self.kind, str(self.device)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CameraError(f"The vision code did not finish: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no details"
            raise CameraError(f"The vision code failed: {detail}")
        try:
            reading = json.loads(result.stdout.strip().splitlines()[-1])
            return BoardObservation(frozenset(reading["occupied"]), tuple(reading["uncertain"]))
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise CameraError(f"The vision code gave an unreadable answer: {result.stdout[-200:]!r}") from exc


def find_vision_dir() -> Path | None:
    """The pi-only checkout, if it is on this machine."""
    configured = os.environ.get("CHESS_VISION_DIR", "").strip()
    for candidate in ([configured] if configured else DEFAULT_VISION_DIRS):
        vision_dir = Path(candidate).expanduser()
        if (vision_dir / "board_vision.py").is_file():
            return vision_dir
        if configured or vision_dir.is_dir():
            logging.error("CAMERA off: no board_vision.py in %s; it should be a checkout of the pi-only branch", vision_dir)
    return None


def camera_from_env():
    """The camera, or None when it cannot work yet, with the reason in the service log."""
    vision_dir = find_vision_dir()
    if vision_dir is None:
        return None
    python = Path(os.environ.get("CHESS_VISION_PYTHON") or vision_dir / ".venv" / "bin" / "python").expanduser()
    calibration = Path(os.environ.get("CHESS_CALIBRATION_DIR") or vision_dir / "calibration").expanduser()
    if not python.exists():
        logging.warning("CAMERA off: %s does not exist; create that .venv from the checkout's requirements.txt", python)
        return None
    if not (calibration / "calibration.json").is_file():
        logging.warning("CAMERA off: no calibration in %s; run pi_game.py setup at the board, then restart", calibration)
        return None
    return VisionCamera(vision_dir, calibration, os.environ.get("CHESS_CAMERA", "pi"),
                        int(os.environ.get("CHESS_CAMERA_DEVICE", "0")), python)
