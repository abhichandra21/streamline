"""Back up the owner's state: the database, settings, keys, overrides, manual lists and AI-written files.

Each backup is one zip in data/backups, plus an optional copy in another folder
(a NAS share, a USB drive) and an optional upload through rclone (Google Drive,
Dropbox, S3 and the rest). Every place keeps the newest few. Of the cache, what
the AI wrote (taste profile, taste rows, title notes), the watch index and TMDB's
title details are kept; the rest (posters, providers, IMDb ratings, release
dates) is fetched again on its own.

Restore: stop the app, unzip a backup into the checkout (every file sits at its
usual path), start the app.
"""
import argparse
import fcntl
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import config

log = logging.getLogger("recommender.backup")

BACKUP_DIR = Path(config.EVENT_DB_PATH).parent / "backups"
NAME_RE = re.compile(r"^streamline-backup-\d{8}-\d{6}\.zip$")
JOB_LABEL = "backup"
DUE_AFTER = timedelta(hours=24)
RCLONE_TIMEOUT_SECONDS = 600


class BackupError(Exception):
    pass


def _files_to_save() -> list[tuple[Path, str]]:
    """(path on disk, path inside the zip) for each file that exists, database aside."""
    root = Path(config.__file__).parent
    candidates = [
        (root / "config.local.yaml", "config.local.yaml"),
        # The owner chose to keep keys in backups, unencrypted (#152).
        (root / ".env", ".env"),
        (Path(config.KEYS_PATH), "data/keys.env"),
        (Path(config.OVERRIDES_PATH), "data/overrides.json"),
        (Path(config.MANUAL_TV_PATH), "data/manual/tv.csv"),
        (Path(config.MANUAL_MOVIES_PATH), "data/manual/movies.csv"),
        # Written by the AI: rebuilding costs money and gives different words. The watch
        # index comes too, so a restored app counts as set up without the exports.
        *[(Path(p), f"recommender/cache/{Path(p).name}") for p in (
            config.WATCH_INDEX_PATH, config.TASTE_PROFILE_PATH, config.STRUCTURED_TASTE_PROFILE_PATH, config.TASTE_TAGS_PATH,
            config.TASTE_THEMES_PATH, config.TASTE_WORDS_PATH, config.TASTE_PLACEMENTS_PATH)],
    ]
    # Title notes the AI wrote, and TMDB's details for each title: Rate It and the
    # Archive read those from the cache only, and setup can't refetch them without the exports.
    for folder, arcdir in ((Path(config.ENRICHMENT_CACHE_DIR), "recommender/cache/enrichments"),
                           (Path(config.CACHE_DIR) / "movie", "recommender/cache/tmdb/movie"),
                           (Path(config.CACHE_DIR) / "tv", "recommender/cache/tmdb/tv")):
        if folder.is_dir():
            candidates += [(p, f"{arcdir}/{p.relative_to(folder)}")
                           for p in sorted(folder.rglob("*")) if p.is_file()]
    # Keep each file's own path inside the checkout, so unzipping puts it back.
    return [(path, str(path.relative_to(root)) if path.is_relative_to(root) else arcname)
            for path, arcname in candidates if path.is_file()]


def _write_zip(target: Path) -> None:
    db_path = Path(config.EVENT_DB_PATH)
    with tempfile.TemporaryDirectory(dir=BACKUP_DIR) as tmp:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            if db_path.is_file():
                # SQLite's backup API copies a consistent snapshot even while the app writes.
                copy = Path(tmp) / "streamline.db"
                src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                dst = sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                zf.write(copy, "data/streamline.db")
            for path, arcname in _files_to_save():
                zf.write(path, arcname)


def _prune_folder(folder: Path, keep: int) -> None:
    names = sorted(p.name for p in folder.iterdir() if NAME_RE.match(p.name))
    for name in names[:-keep]:
        (folder / name).unlink()


def _copy_to_folder(source: Path, folder: Path, keep: int) -> None:
    if not folder.is_dir():
        raise BackupError(f"{folder} is not a folder the server can see. Is the drive mounted?")
    partial = folder / (source.name + ".partial")
    shutil.copy2(source, partial)
    partial.replace(folder / source.name)
    _prune_folder(folder, keep)


def _rclone(*args: str) -> str:
    if not shutil.which("rclone"):
        raise BackupError("rclone is not installed on the server.")
    try:
        result = subprocess.run(["rclone", *args], capture_output=True, text=True,
                                timeout=RCLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"rclone took longer than {RCLONE_TIMEOUT_SECONDS // 60} minutes.") from exc
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise BackupError(lines[-1] if lines else f"rclone exited with status {result.returncode}.")
    return result.stdout


def _upload(source: Path, remote: str, keep: int) -> None:
    remote = remote.rstrip("/")
    _rclone("copyto", str(source), f"{remote}/{source.name}")
    names = sorted(n for n in _rclone("lsf", "--files-only", remote).splitlines() if NAME_RE.match(n))
    for name in names[:-keep]:
        _rclone("deletefile", f"{remote}/{name}")


def make_backup(now: datetime | None = None) -> dict:
    """Make one backup and send it to every place set in Settings; returns the saved status.

    The local zip must succeed. A failing copy or upload is recorded in the
    status and does not stop the others.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(BACKUP_DIR / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise BackupError("A backup is already running.")
    try:
        now = now or datetime.now()
        name = f"streamline-backup-{now:%Y%m%d-%H%M%S}.zip"
        target = BACKUP_DIR / name
        partial = BACKUP_DIR / (name + ".partial")
        try:
            _write_zip(partial)
            partial.replace(target)
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            partial.unlink(missing_ok=True)
            # Keep the last good backup's details, and say this one failed.
            status = last_status() or {}
            status["failure"] = {"at": now.isoformat(timespec="seconds"), "error": str(exc)}
            _save_status(status)
            raise BackupError(f"Could not write the backup: {exc}") from exc
        keep = max(1, int(config.BACKUP_KEEP))
        _prune_folder(BACKUP_DIR, keep)

        status = {"at": now.isoformat(timespec="seconds"), "file": name,
                  "size_bytes": target.stat().st_size, "destinations": {}}
        if config.BACKUP_FOLDER:
            status["destinations"]["folder"] = _try(
                config.BACKUP_FOLDER, lambda: _copy_to_folder(target, Path(config.BACKUP_FOLDER), keep))
        if config.BACKUP_RCLONE_REMOTE:
            status["destinations"]["cloud"] = _try(
                config.BACKUP_RCLONE_REMOTE, lambda: _upload(target, config.BACKUP_RCLONE_REMOTE, keep))
        _save_status(status)
        return status
    finally:
        lock.close()


def _save_status(status: dict) -> None:
    tmp_status = BACKUP_DIR / "last_backup.json.tmp"
    tmp_status.write_text(json.dumps(status, indent=2))
    tmp_status.replace(BACKUP_DIR / "last_backup.json")


def _try(where: str, action) -> dict:
    try:
        action()
        return {"where": where, "ok": True, "error": None}
    except (BackupError, OSError) as exc:
        log.warning("Backup copy to %s failed: %s", where, exc)
        return {"where": where, "ok": False, "error": str(exc)}


def last_status() -> dict | None:
    try:
        return json.loads((BACKUP_DIR / "last_backup.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_due(now: datetime | None = None) -> bool:
    status = last_status()
    if not status or "at" not in status:
        return True
    try:
        last = datetime.fromisoformat(status["at"])
    except (KeyError, ValueError):
        return True
    return (now or datetime.now()) - last >= DUE_AFTER


def list_backups() -> list[dict]:
    """Saved backups on the server, newest first."""
    if not BACKUP_DIR.is_dir():
        return []
    rows = []
    for path in BACKUP_DIR.iterdir():
        if NAME_RE.match(path.name):
            stamp = datetime.strptime(path.name[len("streamline-backup-"):-len(".zip")], "%Y%m%d-%H%M%S")
            rows.append({"name": path.name, "at": stamp, "size_bytes": path.stat().st_size})
    return sorted(rows, key=lambda r: r["name"], reverse=True)


def rclone_installed() -> bool:
    return shutil.which("rclone") is not None


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="recommend backup", description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        status = make_backup()
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {BACKUP_DIR / status['file']}")
    failed = False
    for result in status["destinations"].values():
        if result["ok"]:
            print(f"Copied to {result['where']}")
        else:
            failed = True
            print(f"Copy to {result['where']} failed: {result['error']}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
