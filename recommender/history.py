"""Query history storage.

Stores the last N queries with results, timestamps, and usage stats
in a JSON file under the cache directory. Gitignored (personal data).
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

import config
from .models import Recommendation

log = logging.getLogger("recommender.history")

HISTORY_PATH = Path(config.ENRICHMENT_CACHE_DIR).parent / "query_history.json"
MAX_ENTRIES = 100
_HISTORY_LOCK = Lock()


def _lock_file(history_file, *, exclusive: bool) -> None:
    if fcntl is not None:
        lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(history_file.fileno(), lock_mode)


def _unlock_file(history_file) -> None:
    if fcntl is not None:
        fcntl.flock(history_file.fileno(), fcntl.LOCK_UN)


def _with_locked_history_file(action):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_LOCK:
        with HISTORY_PATH.open("a+", encoding="utf-8") as history_file:
            _lock_file(history_file, exclusive=True)
            try:
                return action(history_file)
            finally:
                _unlock_file(history_file)


def _read_history_file(action):
    if not HISTORY_PATH.exists():
        return []
    with _HISTORY_LOCK:
        try:
            with HISTORY_PATH.open("r", encoding="utf-8") as history_file:
                _lock_file(history_file, exclusive=False)
                try:
                    return action(history_file)
                finally:
                    _unlock_file(history_file)
        except OSError as exc:
            log.warning("Failed to read query history from %s: %s", HISTORY_PATH, exc)
            return []


def _load_raw_from_file(history_file) -> list[dict]:
    history_file.seek(0)
    raw_text = history_file.read()
    if raw_text:
        try:
            return json.loads(raw_text)
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt query history, resetting.")
            return []
    return []


def _load_raw() -> list[dict]:
    return _read_history_file(_load_raw_from_file)


def _write_raw_to_file(history_file, entries: list[dict]) -> None:
    history_file.seek(0)
    history_file.truncate()
    json.dump(entries, history_file, indent=2)
    history_file.flush()
    os.fsync(history_file.fileno())


def record(
    query: str,
    results: list[Recommendation],
    provider: str,
    usage_summary: str,
) -> None:
    """Append a query + results to history, capped at MAX_ENTRIES."""
    def append_entry(history_file) -> None:
        entries = _load_raw_from_file(history_file)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "provider": provider,
            "results": [
                {
                    "title": r.title,
                    "content_type": r.content_type,
                    "score": round(r.score, 3),
                    "vote_average": r.vote_average,
                    "explanation": r.explanation,
                    "streaming_providers": r.streaming_providers[:4],
                }
                for r in results
            ],
            "usage": usage_summary,
        }
        entries.append(entry)

        if len(entries) > MAX_ENTRIES:
            entries[:] = entries[-MAX_ENTRIES:]

        _write_raw_to_file(history_file, entries)

    _with_locked_history_file(append_entry)


def delete(timestamp: str) -> bool:
    """Delete a history entry by its timestamp. Returns True if found and removed."""
    def remove_entry(history_file) -> bool:
        entries = _load_raw_from_file(history_file)
        before = len(entries)
        entries = [e for e in entries if e.get("timestamp") != timestamp]
        if len(entries) == before:
            return False
        _write_raw_to_file(history_file, entries)
        return True

    return _with_locked_history_file(remove_entry)


def load(limit: int | None = None) -> list[dict]:
    """Load history entries, most recent first."""
    entries = _load_raw()
    entries.reverse()
    if limit:
        entries = entries[:limit]
    return entries
