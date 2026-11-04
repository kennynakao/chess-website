"""See the physical board, so the game can check it against what should be there.

The vision code lives on the pi-only branch of Self-Moving-Chess-Board-Code
(board_vision.py and pi_camera.py). VisionCamera loads it from a checkout on the Pi:

    CHESS_VISION_DIR       that checkout; without it the service runs with no camera
    CHESS_CALIBRATION_DIR  what `pi_game.py setup` wrote (default: calibration/ in the checkout)
    CHESS_CAMERA           "pi" (default) or "usb"
    CHESS_CAMERA_DEVICE    camera index (default 0)

The service's virtual environment also needs that branch's requirements, for OpenCV.

A camera only sees which squares are occupied, not which piece is on them. Moves that
leave the same squares occupied, such as the four promotions, therefore come back as
"ambiguous" and someone has to choose.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import chess


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


class VisionCamera:
    """Adapter for the pi-only branch's camera and vision modules."""
    name = "vision"

    def __init__(self, vision_dir: str | Path, calibration_dir: str | Path | None = None,
                 kind: str = "pi", device: int = 0) -> None:
        vision_dir = Path(vision_dir).expanduser()
        if str(vision_dir) not in sys.path:
            sys.path.insert(0, str(vision_dir))
        # Imported here so the service needs no OpenCV unless a camera is configured.
        from board_vision import load_calibration, observe, read_image
        from pi_camera import Camera
        self.directory = Path(calibration_dir).expanduser() if calibration_dir else vision_dir / "calibration"
        self._observe, self._read_image = observe, read_image
        self.calibration, self.reference = load_calibration(self.directory)
        self.camera = Camera(kind, device)

    def observe(self) -> BoardObservation:
        # One file, replaced each time: this service stays up, so photos must not pile up.
        path = self.camera.capture(self.directory / "latest.jpg")
        observation, _ = self._observe(self.reference, self._read_image(path), self.calibration)
        return BoardObservation(frozenset(observation.occupied), tuple(observation.uncertain))


def camera_from_env():
    vision_dir = os.environ.get("CHESS_VISION_DIR", "").strip()
    if not vision_dir:
        return None
    try:
        return VisionCamera(vision_dir, os.environ.get("CHESS_CALIBRATION_DIR") or None,
                            os.environ.get("CHESS_CAMERA", "pi"), int(os.environ.get("CHESS_CAMERA_DEVICE", "0")))
    except Exception:
        # Keep the game playable without board checks, and say why in the service log.
        logging.exception("CAMERA unavailable; running without board checks")
        return None
