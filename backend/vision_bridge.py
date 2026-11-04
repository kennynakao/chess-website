#!/usr/bin/env python3
"""Take one photo with the pi-only code and print what it sees, as one line of JSON.

This runs under that checkout's own Python, which has OpenCV, and not under the game
service's. Exits non-zero, with the reason on standard error, if no photo could be read.

Usage: vision_bridge.py VISION_DIR CALIBRATION_DIR CAMERA DEVICE
"""

import json
from pathlib import Path
import sys


def main(vision_dir: str, calibration_dir: str, kind: str, device: str) -> None:
    sys.path.insert(0, vision_dir)
    from board_vision import load_calibration, observe, read_image
    from pi_camera import Camera

    directory = Path(calibration_dir)
    calibration, reference = load_calibration(directory)
    # One file, replaced each time: the game service stays up, so photos must not pile up.
    path = Camera(kind, int(device)).capture(directory / "latest.jpg")
    observation, _ = observe(reference, read_image(path), calibration)
    print(json.dumps({"occupied": sorted(observation.occupied), "uncertain": list(observation.uncertain)}))


if __name__ == "__main__":
    main(*sys.argv[1:])
