"""The gantry that will move the pieces: NOT DONE YET. This file is its placeholder.

The plan is Pi-to-Arduino. The Pi (this service) sends each step to an Arduino over USB
serial, and the Arduino drives the gantry motors and the magnet or gripper. None of that
exists yet, so this driver refuses every job with a clear message instead of pretending.
Until the real code is here, website board games run with the default driver, which logs
each job and carries on, or with CHESS_MOTION_COMMAND pointing at a motor program.

Where this sits: remote_server.py runs a website board game as

    player's move -> gantry -> camera -> Stockfish -> gantry -> camera -> back to the player

and board_pipeline.py hands this driver one job per chess move. board_motion.py documents
the job: {"id": 7, "label": "e7e5", "expect": [...occupied squares...], "steps": [...]},
where each step is {"op": "carry", "piece": "p", "from": "e7", "to": "e5",
"path": [[4, 6], [4, 4]]} with points in squares (a1 is 0,0; x.5 are the lanes between
squares; captured pieces park at x = -1, -2 and 8, 9), or {"op": "note", "message": ...}.

What the finished driver has to do, per job:

  1. Open the serial port once and keep it open (CHESS_GANTRY_PORT, default /dev/ttyACM0;
     CHESS_GANTRY_BAUD, default 115200).
  2. For each "carry" step: go to path[0], pick the piece up, follow the rest of the path,
     put it down. Converting squares to motor steps or millimetres belongs in the Arduino
     sketch or a settings file, not in the chess code.
  3. Wait for the Arduino to confirm each step. An error, a limit switch or a timeout
     raises, which stops the game until someone checks the board and confirms on the
     website.
  4. Return only when every piece has stopped moving: the camera photographs the board
     right after this returns.

Select it with CHESS_GANTRY=arduino in relay.env. Leave that unset until the code exists.
The in-person game on the touchscreen (pi_game.py and pi_touch.py on the pi-only branch)
has nothing to do with this file.
"""

from __future__ import annotations

import logging
import os

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
NOT_DONE = "The Pi-to-Arduino gantry code is not written yet."


class ArduinoGantry:
    """Placeholder for the Pi-to-Arduino gantry driver. Every job stops the game with NOT_DONE."""
    name = "arduino"
    # Once written, this driver really moves pieces, so the camera checks after it.
    moves_pieces, needs_person = True, False

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD) -> None:
        self.port, self.baud = port, baud

    def execute(self, job: dict) -> None:
        # TODO(gantry): open self.port at self.baud, send each step, wait for the Arduino.
        logging.error("PIPELINE %s gantry: %s (would use %s at %s baud)", job["label"], NOT_DONE, self.port, self.baud)
        raise NotImplementedError(NOT_DONE)


def arduino_from_env() -> ArduinoGantry:
    port = os.environ.get("CHESS_GANTRY_PORT", "").strip() or DEFAULT_PORT
    try:
        baud = int(os.environ.get("CHESS_GANTRY_BAUD", "").strip() or DEFAULT_BAUD)
    except ValueError:
        logging.warning("GANTRY CHESS_GANTRY_BAUD is not a number; using %s", DEFAULT_BAUD)
        baud = DEFAULT_BAUD
    return ArduinoGantry(port, baud)
