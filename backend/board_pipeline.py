"""The website board game: one move at a time through the gantry, the camera and Stockfish.

The Pi has three separate ways to play, and this module is only the third:

  * In person on the touchscreen, player against the bot, with pi_game.py and pi_touch.py
    from the pi-only branch. That program has its own game and this service never touches it.
  * The digital game on the website: Stockfish answers inside the same request and no
    piece moves. remote_server.py handles that by itself.
  * The board game on the website, started with "board": true: every move runs

        player's move -> gantry -> camera -> Stockfish -> gantry -> camera -> back to the player

    and each stage starts only when the one before it has finished. That is this module.

    The gantry never takes a captured piece off the board. A job whose steps hold a "remove"
    (see board_motion.py) stops there, in state "waiting" with stage "remove", until the
    person at the board has lifted the piece off and pressed Check (/api/board-confirm with
    the job's id): each press takes a photo, and the carry follows once the camera has seen
    the square empty. Without a camera a press is trusted; "override" skips the photo.

remote_server.py owns the chess game and its record. This module owns the motion_jobs queue
in the same database and the worker thread that performs it. The gantry is whichever driver
board_motion.driver_from_env() picked; backend/gantry.py runs the gantry branch's Pi driver
once it is configured. The camera is board_camera.camera_from_env().
Stockfish's stage is the only one that changes the game record, so the pipeline calls back
into the service for it (reply_owed and play_reply). Every stage is logged as PIPELINE.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Callable

from backend.board_camera import compare
from backend.board_motion import SimulatedDriver, describe_job, describe_step

CAMERA_DISAGREES = "The camera does not see the board the game expects."
MOVE_FAILED = "The board could not finish this move."
RESTARTED = "The Pi restarted while this move was running."
CAMERA_FAILED = "The camera could not take a photo."


def stored_list(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _seen(missing, extra) -> str:
    """"no piece on e2 and an extra piece on d5", or "" when the lists are empty."""
    parts = []
    if missing:
        parts.append(f"no piece on {', '.join(missing)}")
    if extra:
        parts.append(f"an extra piece on {', '.join(extra)}")
    return " and ".join(parts)


def problem_text(problem, square: str | None = None) -> str | None:
    """What the camera saw wrong, in plain words; `square` is the one a person was asked to clear."""
    if problem is None:
        return None
    if problem.get("error"):
        return problem["error"]
    if problem.get("uncertain"):
        return f"The camera can't tell about {', '.join(problem['uncertain'])}. Try again."
    still_there = square is not None and square in problem.get("extra", [])
    seen = _seen(problem.get("missing", []), [name for name in problem.get("extra", []) if name != square])
    if still_there:
        return f"{square} still has a piece on it." + (f" The camera also sees {seen}." if seen else "")
    return f"The camera sees {seen}." if seen else CAMERA_DISAGREES


def disagreement(problem) -> str:
    """The fault message after a job: names the squares, since a piece that is off the board can only be put back by hand."""
    if problem.get("error") or problem.get("uncertain"):
        return f"{CAMERA_DISAGREES} {problem_text(problem)}"
    seen = _seen(problem.get("missing", []), problem.get("extra", []))
    return f"{CAMERA_DISAGREES[:-1]}: {seen}." if seen else CAMERA_DISAGREES


class BoardPipeline:
    """The queue of physical steps and the worker that runs them. Shares the service's lock."""

    def __init__(self, db: sqlite3.Connection, lock, driver=None, camera=None,
                 reply_owed: Callable[[], bool] = lambda: False, play_reply: Callable[[], bool] = lambda: True):
        self.db, self.lock = db, lock
        self.ready = threading.Condition(lock)
        self.driver = driver or SimulatedDriver()
        self.camera = camera
        self.camera_lock = threading.Lock()
        self.reply_owed, self.play_reply = reply_owed, play_reply
        self.closing = False
        # Which job the person at the board has said is done, and what the camera last saw wrong.
        self.confirmed_job, self.board_override = None, False
        self.abandoned_job = None  # Deleted by a new game while the worker was waiting on it.
        self.mismatch = None
        self.stage = None  # "gantry", "remove", "camera" or "stockfish" while that stage runs.
        # {"jobId", "square", "instructions"} while the worker waits for a person to lift a captured piece off.
        self.removal = None
        self.db.execute("CREATE TABLE IF NOT EXISTS motion_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, steps TEXT NOT NULL, expect TEXT NOT NULL, status TEXT NOT NULL, error TEXT, context TEXT)")
        if "context" not in {row[1] for row in self.db.execute("PRAGMA table_info(motion_jobs)")}:
            # Databases from before the gantry program planned its own paths.
            self.db.execute("ALTER TABLE motion_jobs ADD COLUMN context TEXT")
        # A job cut off by a restart may have left a piece anywhere: never assume it finished.
        with self.db:
            self.db.execute("UPDATE motion_jobs SET status='failed', error=? WHERE status='running'", (RESTARTED,))
            # A person can simply be asked again.
            self.db.execute("UPDATE motion_jobs SET status='pending' WHERE status='waiting'")
        self.thread = threading.Thread(target=self._run, name="board-pipeline", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        with self.ready:
            self.closing = True
            self.ready.notify()
        self.thread.join(timeout=1)

    # ------------------------------------------------------------------ what the website sees

    def status(self, reply_owed: bool | None = None) -> dict:
        """Under the lock. reply_owed overrides the callback, for a snapshot of a game not yet committed."""
        rows = self.db.execute("SELECT id, label, status, error, steps FROM motion_jobs ORDER BY id").fetchall()
        reply_next = not rows and (self.reply_owed() if reply_owed is None else reply_owed)
        failed = next((row for row in rows if row[2] == "failed"), None)
        current = failed or next((row for row in rows if row[2] == "running"), None) or (rows[0] if rows else None)
        # A captured piece is the person's to lift off once the worker gets to that step; with a person in
        # the gantry's place, their next step is theirs to do from the moment it is queued.
        removal = self.removal if current and not failed and self.removal and self.removal["jobId"] == current[0] else None
        waiting = current if current and not failed and (removal or self.driver.needs_person) else None
        return {
            "mode": self.driver.name,
            "camera": self.camera is not None,
            "state": "fault" if failed else "waiting" if waiting else "moving" if rows or reply_next else "idle",
            "stage": "remove" if removal else None if failed or waiting else self.stage or ("gantry" if rows else "stockfish" if reply_next else None),
            "current": current[1] if current else None,
            "jobId": waiting[0] if waiting else None,
            "pending": sum(row is not current and row[2] == "pending" for row in rows),
            "error": failed[3] if failed else None,
            "instructions": removal["instructions"] if removal else describe_job({"label": waiting[1], "steps": stored_list(waiting[4])}) if waiting else [],
            "mismatch": self.mismatch if waiting else None,
            # What the last Check found wrong, in words; null before the first Check and once the square is clear.
            "problem": problem_text(self.mismatch, removal["square"]) if removal else None,
        }

    # ------------------------------------------------------------------ changes made by the service, under the lock

    def queue(self, job: dict) -> None:
        """Inside the caller's transaction, so a game and the steps it owes the board commit together."""
        context = {key: job[key] for key in ("before", "pieces") if key in job}
        self.db.execute("INSERT INTO motion_jobs (label, steps, expect, status, context) VALUES (?,?,?,'pending',?)",
                        (job["label"], json.dumps(job["steps"]), json.dumps(job["expect"]), json.dumps(context) if context else None))

    def drop_queued(self, failed: bool = False) -> int | None:
        """Inside the caller's transaction: forget steps nobody has done yet. Returns a waiting job's id, to abandon after the commit.

        With failed=True a stopped board is forgotten too: someone has checked the pieces (the
        screen's camera saw the starting position, or a person pressed Reset Board), so the
        failure no longer describes the board.
        """
        waiting = self.db.execute("SELECT id FROM motion_jobs WHERE status='waiting'").fetchone()
        statuses = ("pending", "waiting", "failed") if failed else ("pending", "waiting")
        self.db.execute(f"DELETE FROM motion_jobs WHERE status IN ({','.join('?' * len(statuses))})", statuses)
        return waiting[0] if waiting else None

    def fault(self, label: str, error: str) -> None:
        """Stop the game here with a message for the website."""
        self.stage = None
        with self.db:
            self.db.execute("INSERT INTO motion_jobs (label, steps, expect, status, error) VALUES (?, '[]', '[]', 'failed', ?)", (label, error))

    def resolve(self) -> bool:
        """Someone checked the board and it matches the screen: the queue restarts from here."""
        if self.status()["state"] != "fault":
            return False
        with self.db:
            self.db.execute("DELETE FROM motion_jobs WHERE status IN ('failed', 'pending')")
        logging.info("MOTION resolved by the player; queue cleared")
        self.ready.notify()
        return True

    def confirm(self, job_id: int | None = None, override: bool = False) -> None:
        """The person at the board says the step on screen is done, or presses Check after lifting a piece off.

        With override, the camera is not consulted.
        """
        row = self.db.execute("SELECT id FROM motion_jobs WHERE status IN ('pending', 'waiting') ORDER BY id").fetchone()
        # A press for a step the screen showed earlier must never confirm the one after it.
        if row and (job_id is None or job_id == row[0]):
            self.confirmed_job, self.board_override = row[0], override
            self.ready.notify()

    def observe(self):
        """A photo, read by the pi-only vision code. Not under the lock: a photo takes seconds."""
        with self.camera_lock:
            return self.camera.observe()

    # ------------------------------------------------------------------ the worker

    def _run(self) -> None:
        """Perform queued jobs one at a time, and never die silently: a dead worker looks like a busy board."""
        while True:
            try:
                if not self._perform_next():
                    return
            except Exception:
                logging.exception("MOTION worker error")
                with self.ready:
                    self.stage, self.removal = None, None
                    if self.closing:
                        return
                    try:
                        with self.db:
                            self.db.execute("UPDATE motion_jobs SET status='failed', error=? WHERE status IN ('running', 'waiting')",
                                            ("The board queue hit an error.",))
                    except sqlite3.Error:
                        logging.exception("MOTION could not record the error")
                    self.ready.wait(1)

    def _perform_next(self) -> bool:
        """One stage of a website move, without holding the lock while anything moves. False when closing."""
        with self.ready:
            while not self.closing:
                job = self._next_job()
                if job or self._reply_is_next():
                    break
                self.ready.wait()
            if self.closing:
                return False
            self.mismatch = None
            self.stage = "gantry" if job else "stockfish"
            if job:
                with self.db:
                    self.db.execute("UPDATE motion_jobs SET status=? WHERE id=?",
                                    ("waiting" if self.driver.needs_person else "running", job["id"]))
        if job is None:
            return self.play_reply()
        logging.info("PIPELINE %s gantry: sending %s", job["label"], json.dumps(job))
        error = None
        try:
            if self.driver.needs_person:
                # The person does every step, captured pieces included, and the camera checks the result.
                self.driver.execute(job)
                if not self._wait_for_person(job):
                    return False
            else:
                performed = self._perform_steps(job)
                if performed is None:
                    return False
                if performed and (problem := self._camera_stage(job)):
                    # Which squares, so that after a capture is taken back or the board resets the person
                    # knows what to put back: the gantry cannot fetch pieces that are off the board.
                    error = disagreement(problem)
                    logging.warning("PIPELINE %s camera: disagrees: %s", job["label"], problem)
        except NotImplementedError as exc:
            # The gantry placeholder: say exactly what is missing instead of a generic failure.
            error = str(exc) or MOVE_FAILED
        except Exception:
            error = MOVE_FAILED
            logging.exception("PIPELINE %s failed", job["label"])
        with self.ready:
            self.stage = None
            if self.closing:
                return False
            with self.db:
                if error:
                    self.db.execute("UPDATE motion_jobs SET status='failed', error=? WHERE id=?", (error, job["id"]))
                else:
                    self.db.execute("DELETE FROM motion_jobs WHERE id=?", (job["id"],))
            if not error and self.status()["state"] != "moving":
                logging.info("PIPELINE back to the player")
        return True

    def _perform_steps(self, job: dict):
        """The gantry stage. The carries go to the driver; at a remove step the job waits for the person at the board.

        True once the carries are done, False when the job was abandoned while it waited, None when closing.
        """
        if not self.driver.moves_pieces or not any(step["op"] == "remove" for step in job["steps"]):
            self.driver.execute(job)
            return True
        # The board as the steps go: what the camera should see once a piece is off, and what the gantry
        # program starts each run from. Jobs saved before the gantry got that context have neither.
        pieces = dict(job.get("pieces") or {})
        occupied = set(job.get("before") or pieces)
        steps, start = job["steps"], 0
        for index, step in enumerate(steps):
            if step["op"] != "remove":
                continue
            self._carry(job, steps[start:index], pieces, occupied)
            waited = self._wait_for_removal(job, step, occupied)
            if waited is not True:
                return waited
            pieces.pop(step["square"], None)
            occupied.discard(step["square"])
            start = index + 1
        self._carry(job, steps[start:], pieces, occupied)
        return True

    def _carry(self, job: dict, steps: list, pieces: dict, occupied: set) -> None:
        """One run of the gantry program: the same job, with these steps and the board as it is now."""
        if not any(step["op"] == "carry" for step in steps):
            return
        context = {"before": sorted(pieces), "pieces": dict(pieces)} if pieces else {}
        self.driver.execute({**job, "steps": steps, **context})
        for step in steps:
            if step["op"] == "carry":
                pieces[step["to"]] = pieces.pop(step["from"], step["piece"])
                occupied.discard(step["from"])
                occupied.add(step["to"])

    def _wait_for_removal(self, job: dict, step: dict, occupied: set):
        """Hold the job until the person has lifted the piece off and, with a camera, it has seen the square empty.

        True to carry on, False when the job was abandoned meanwhile, None when closing.
        """
        square = step["square"]
        expected = sorted(occupied - {square})
        with self.ready:
            self.stage = "remove"
            self.removal = {"jobId": job["id"], "square": square, "instructions": [describe_step(step)]}
            self.mismatch = None
            self.confirmed_job = None  # A press before this step was on screen was not for it.
            with self.db:
                self.db.execute("UPDATE motion_jobs SET status='waiting' WHERE id=?", (job["id"],))
        logging.info("PIPELINE %s remove: waiting for the piece on %s to be taken off", job["label"], square)
        try:
            while True:
                with self.ready:
                    while not self.closing and job["id"] not in (self.confirmed_job, self.abandoned_job):
                        self.ready.wait()
                    if self.closing:
                        return None
                    if job["id"] == self.abandoned_job:
                        return False
                    override, self.confirmed_job, self.board_override = self.board_override, None, False
                if override or self.camera is None or not occupied:
                    logging.info("PIPELINE %s remove: %s taken as cleared%s", job["label"], square,
                                 " without a photo" if override else "" if self.camera is None else " (no record of the board to check)")
                    return True
                # A bad look never fails the job: the person is told what the camera sees and presses Check again.
                try:
                    problem = compare(expected, self.observe())
                except Exception:
                    logging.exception("CAMERA failed during job=%s", job["id"])
                    problem = {"error": CAMERA_FAILED}
                with self.lock:
                    self.mismatch = problem
                if problem is None:
                    logging.info("PIPELINE %s remove: the camera sees %s empty", job["label"], square)
                    return True
                logging.warning("PIPELINE %s remove: %s", job["label"], problem_text(problem, square))
        finally:
            with self.ready:
                self.removal, self.mismatch, self.stage = None, None, "gantry"
                if not self.closing:
                    with self.db:
                        self.db.execute("UPDATE motion_jobs SET status='running' WHERE id=? AND status='waiting'", (job["id"],))

    def _camera_stage(self, job: dict):
        """Check the board after the gantry. Returns what is wrong, or None."""
        if not self.driver.moves_pieces:
            logging.info("PIPELINE %s camera: skipped, nothing moved because the gantry is not connected", job["label"])
            return None
        if self.camera is None:
            logging.info("PIPELINE %s camera: not connected, skipped", job["label"])
            return None
        with self.lock:
            self.stage = "camera"
        problem = self._check_board(job)
        if problem is None:
            logging.info("PIPELINE %s camera: confirmed", job["label"])
        return problem

    def _reply_is_next(self) -> bool:
        return self.reply_owed() and not self.db.execute("SELECT 1 FROM motion_jobs").fetchone()

    def _check_board(self, job: dict):
        return compare(job["expect"], self.observe()) if self.camera else None

    def _wait_for_person(self, job: dict) -> bool:
        """Hold the job until someone confirms and, if there is a camera, it agrees. False when closing."""
        while True:
            with self.ready:
                while not self.closing and job["id"] not in (self.confirmed_job, self.abandoned_job):
                    self.ready.wait()
                if self.closing:
                    return False
                if job["id"] == self.abandoned_job:
                    return True
                override, self.confirmed_job, self.board_override = self.board_override, None, False
            if override:
                return True
            # Like a retaken photo in pi_game.py: a bad look never fails the job, it just asks again.
            try:
                problem = self._check_board(job)
            except Exception:
                logging.exception("CAMERA failed during job=%s", job["id"])
                problem = {"error": CAMERA_FAILED}
            with self.lock:
                self.mismatch = problem
            if problem is None:
                return True

    def _next_job(self):
        # A failure halts everything behind it: later steps assume the earlier ones happened.
        if self.db.execute("SELECT 1 FROM motion_jobs WHERE status='failed'").fetchone():
            return None
        row = self.db.execute("SELECT id, label, steps, expect, context FROM motion_jobs WHERE status='pending' ORDER BY id").fetchone()
        if not row:
            return None
        steps, expect = stored_list(row[2]), stored_list(row[3])
        if steps is None or expect is None:
            logging.error("MOTION job=%s label=%s is unreadable", row[0], row[1])
            with self.db:
                self.db.execute("UPDATE motion_jobs SET status='failed', error='A saved board step could not be read.' WHERE id=?", (row[0],))
            return None
        job = {"id": row[0], "label": row[1], "steps": steps, "expect": expect}
        try:
            context = json.loads(row[4]) if row[4] else {}
        except ValueError:
            context = {}
        if isinstance(context, dict):
            job.update({key: value for key, value in context.items() if key in ("before", "pieces")})
        return job
