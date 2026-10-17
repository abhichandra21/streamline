"""One setup run at a time, a record of the last one, and starting one from the web.

Every setup run (terminal, the weekly timer, the Settings page) holds a file
lock for its whole life, so a second run stops at once instead of racing the
first. The lock lifts by itself when the process ends, however it ends.

The run also writes a small status file when it starts and when it ends, so
the Settings page can show the last run whoever started it. A status that
started but never finished, with the lock free, means the process was killed.
"""

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import config

_CACHE_ROOT = Path(config.TASTE_PROFILE_PATH).parent
LOCK_PATH = _CACHE_ROOT / "setup.lock"
STATUS_PATH = _CACHE_ROOT / "setup_status.json"
LOG_DIR = Path(config.APP_LOG_PATH).parent
# How long start() waits for the new run to take the lock.
START_WAIT_SECONDS = 10
# Tells the setup process where the web app sent its output, for the status file.
LOG_ENV = "STREAMLINE_SETUP_LOG"

# What the Settings page can start, and the setup flags for each.
ACTIONS = {
    "build": [],
    "check_exports": ["--check-exports"],
    "refresh_data": ["--refresh-data"],
    "refresh_profile": ["--refresh-profile"],
    "rethink_themes": ["--rethink-themes"],
    "refresh_imdb": ["--refresh-imdb"],
}
# These make an AI call per title or rebuild the prose profile, so they cost money.
PAID_ACTIONS = {"build", "refresh_profile", "rethink_themes"}

_held_lock = None


def action_for(argv: list[str]) -> str:
    """The ACTIONS name for a setup command line (flags only, as in sys.argv[1:])."""
    flags = {arg for arg in argv if arg.startswith("--")}
    if "--ingest-only" in flags:
        return "import_exports"  # terminal only
    for name, action_flags in ACTIONS.items():
        if action_flags and action_flags[0] in flags:
            return name
    return "build"


def acquire_lock() -> bool:
    """Take the setup lock for the rest of this process, or False if a run holds it."""
    global _held_lock
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _held_lock = handle
    return True


def is_running() -> bool:
    """Whether some process holds the setup lock right now."""
    try:
        with open(LOCK_PATH, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        return False
    return False


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return None


def _write_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(status))
    os.replace(tmp, STATUS_PATH)


def record_start(action: str) -> dict:
    status = {
        "action": action,
        "started_at": time.time(),
        "finished_at": None,
        "ok": None,
        "log": os.environ.get(LOG_ENV),
    }
    _write_status(status)
    return status


def record_end(status: dict, ok: bool) -> None:
    _write_status({**status, "finished_at": time.time(), "ok": ok})


def start(action: str) -> bool:
    """Start setup for action as its own process. False if a run is already going."""
    if is_running():
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"setup-{datetime.now():%Y%m%d-%H%M%S}.log"
    with open(log_path, "w") as log_file:
        # Its own session, so it is not tied to the request that started it.
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "recommender.setup", *ACTIONS[action]],
            cwd=Path(config.__file__).parent,
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            # INFO keeps the progress box readable when the config asks for DEBUG.
            env={**os.environ, LOG_ENV: str(log_path), "LOG_LEVEL": "INFO"},
            start_new_session=True,
        )
    # Collect its exit status when it ends, so no finished run is left as a zombie.
    threading.Thread(target=process.wait, daemon=True).start()
    # Wait until it holds the lock, so the page it returns to shows this run.
    deadline = time.time() + START_WAIT_SECONDS
    while time.time() < deadline and process.poll() is None and not is_running():
        time.sleep(0.1)
    return True


def log_tail(status: dict | None, lines: int = 12) -> list[str]:
    """The last lines of the run's log, when the web app started it."""
    path = (status or {}).get("log")
    if not path:
        return []
    try:
        with open(path, errors="replace") as f:
            return [line.rstrip() for line in f.readlines()[-lines:]]
    except OSError:
        return []


def current() -> dict:
    """The last run as the Settings page shows it."""
    status = read_status()
    running = is_running()
    state = None
    if status:
        if running and status.get("finished_at") is None:
            state = "running"
        elif status.get("finished_at") is None:
            state = "stopped"
        else:
            state = "done" if status.get("ok") else "failed"
    return {"running": running, "state": state, "status": status, "log_tail": log_tail(status)}
