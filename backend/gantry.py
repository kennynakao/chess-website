"""The gantry that moves the pieces: the Pi driver from the `gantry` branch, run once per job.

The gantry code lives in Self-Moving-Chess-Board-Code on branch `gantry`: ESP32 firmware
that speaks GRBL-style serial, and `gantry/pi/gantry_driver.py`, which takes one job as JSON
on standard input, drives the magnet along each step's path, and exits 0 when every piece
has stopped. That is exactly the CommandDriver contract in board_motion.py, so this module
only has to find the program and build its command line:

    <python> <dir>/gantry_driver.py --port <port> --config <config> --quiet

  * <dir>     CHESS_GANTRY_DIR, else the first of ~/chess-gantry/gantry/pi and
              ~/Self-Moving-Chess-Board-Code/gantry/pi that holds gantry_driver.py.
  * <port>    CHESS_GANTRY_PORT, the ESP32's serial port, such as /dev/ttyUSB0. Required.
  * <config>  CHESS_GANTRY_CONFIG, else <dir>/gantry_config.json (square size, a1, speeds).
  * <python>  CHESS_GANTRY_PYTHON, else the pi-only checkout's ~/chess-board-pi-only/.venv/bin/python
              when it exists (it has pyserial), else the interpreter running this service.

Select it with CHESS_GANTRY=arduino in relay.env. Until both a port and the program are
configured, the placeholder below refuses every job with NOT_DONE instead of pretending, so
a board game stops at its first move and says what is missing; digital games never notice.

Where this sits: remote_server.py runs a website board game as

    player's move -> gantry -> camera -> Stockfish -> gantry -> camera -> back to the player

and board_pipeline.py hands the driver one job per chess move. board_motion.py documents the
job: {"id": 7, "label": "e7e5", "expect": [...occupied squares...], "before": [...],
"pieces": {...}, "steps": [...]}, where each step is {"op": "carry", "piece": "p",
"from": "e7", "to": "e5", "path": [[4, 6], [4, 4]]} with points in squares (a1 is 0,0; x.5
are the lanes between squares), or {"op": "note", "message": ...}. Captured pieces are lifted
off by hand: the pipeline holds a job at its {"op": "remove", ...} step until the camera has
seen the square empty, and runs the program only with the carries that follow, so the program
never sees a remove step. The in-person game on the touchscreen (pi_game.py and pi_touch.py on
the pi-only branch) has nothing to do with this file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shlex
import sys

from backend.board_motion import CommandDriver

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DRIVER_SCRIPT = "gantry_driver.py"
DEFAULT_CONFIG = "gantry_config.json"
# Where a checkout of the gantry branch usually is on the Pi, in the order they are tried.
DEFAULT_DIRS = ("~/chess-gantry/gantry/pi", "~/Self-Moving-Chess-Board-Code/gantry/pi")
# The pi-only checkout's environment has pyserial; the service's own may not.
PI_VENV_PYTHON = "~/chess-board-pi-only/.venv/bin/python"
NOT_DONE = ("The gantry is not set up: set CHESS_GANTRY_PORT to the ESP32's serial port and "
            "check out the gantry branch (or set CHESS_GANTRY_DIR) in relay.env.")


class ArduinoGantry:
    """Placeholder while the gantry is not configured. Every job stops the game with NOT_DONE."""
    name = "arduino"
    # Once configured, the real driver moves pieces, so the camera checks after it.
    moves_pieces, needs_person = True, False

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD) -> None:
        self.port, self.baud = port, baud

    def execute(self, job: dict) -> None:
        logging.error("PIPELINE %s gantry: %s (would use %s at %s baud)", job["label"], NOT_DONE, self.port, self.baud)
        raise NotImplementedError(NOT_DONE)


class GantryDriver(CommandDriver):
    """gantry_driver.py from the gantry branch, run once per job like any CHESS_MOTION_COMMAND program.

    Its own name, so the website says the gantry is connected rather than "code not written yet".
    """
    name = "gantry"

    def __init__(self, python: str, directory: Path, port: str, config: str) -> None:
        self.python, self.directory, self.port, self.config = python, directory, port, config
        parts = [python, str(directory / DRIVER_SCRIPT), "--port", port, "--config", config, "--quiet"]
        super().__init__(" ".join(shlex.quote(part) for part in parts))


def gantry_dir() -> Path | None:
    """The folder holding gantry_driver.py, or None. CHESS_GANTRY_DIR, else the usual checkouts."""
    configured = os.environ.get("CHESS_GANTRY_DIR", "").strip()
    candidates = [configured] if configured else list(DEFAULT_DIRS)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if (path / DRIVER_SCRIPT).is_file():
            return path
    return None


def gantry_python() -> str:
    configured = os.environ.get("CHESS_GANTRY_PYTHON", "").strip()
    if configured:
        return configured
    venv = Path(PI_VENV_PYTHON).expanduser()
    return str(venv) if venv.is_file() else sys.executable


def arduino_from_env() -> GantryDriver | ArduinoGantry:
    """The driver CHESS_GANTRY=arduino means: the real program when it is configured, else the placeholder."""
    port = os.environ.get("CHESS_GANTRY_PORT", "").strip()
    directory = gantry_dir()
    if port and directory is not None:
        config = os.environ.get("CHESS_GANTRY_CONFIG", "").strip() or str(directory / DEFAULT_CONFIG)
        driver = GantryDriver(gantry_python(), directory, port, config)
        logging.info("GANTRY %s", " ".join(shlex.quote(part) for part in driver.args))
        return driver
    missing = " and ".join(what for what, absent in (("CHESS_GANTRY_PORT", not port), (DRIVER_SCRIPT, directory is None)) if absent)
    logging.warning("GANTRY not set up (%s missing): board games stop at their first move", missing)
    try:
        baud = int(os.environ.get("CHESS_GANTRY_BAUD", "").strip() or DEFAULT_BAUD)
    except ValueError:
        logging.warning("GANTRY CHESS_GANTRY_BAUD is not a number; using %s", DEFAULT_BAUD)
        baud = DEFAULT_BAUD
    return ArduinoGantry(port or DEFAULT_PORT, baud)
