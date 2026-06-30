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


def _serialize_result(r) -> dict:
    """Serialize a result for storage — accepts enriched dicts or Recommendation objects."""
    if isinstance(r, dict):
        return {
            "title": r["title"],
            "content_type": r["content_type"],
            "score": round(r.get("score") or 0, 3),
            "vote_average": r.get("vote_average") or 0,
            "genres": r.get("genres") or [],
            "explanation": r.get("explanation") or "",
            "streaming_providers": (r.get("streaming_providers") or [])[:4],
            "tmdb_id": r.get("tmdb_id"),
            "poster": r.get("poster"),
            "tmdb_url": r.get("tmdb_url") or "",
            "imdb_url": r.get("imdb_url") or "",
        }
    # Raw Recommendation object
    return {
        "title": r.title,
        "content_type": r.content_type,
        "score": round(r.score, 3),
        "vote_average": r.vote_average,
        "genres": getattr(r, "genres", []),
        "explanation": r.explanation,
        "streaming_providers": r.streaming_providers[:4],
        "tmdb_id": None,
        "poster": None,
        "tmdb_url": "",
        "imdb_url": "",
    }


# Metadata keys a caller may attach to a history entry. Explicit allowlist so
# arbitrary keys cannot leak into the stored record.
_ALLOWED_METADATA_KEYS = frozenset({
    "source", "label", "summary", "intent_dict", "context_note", "refinement",
})


def record(
    query: str,
    results: list,
    provider: str,
    usage_summary: str,
    *,
    metadata: dict | None = None,
) -> None:
    """Append a query + results to history, capped at MAX_ENTRIES.

    ``results`` may be enriched dicts (from web.py) or raw Recommendation objects.
    ``metadata`` carries optional structured fields (e.g. wizard source/label/intent)
    merged into the entry under an explicit allowlist. ``query`` is always kept for
    backward compatibility with old entries.
    """
    def append_entry(history_file) -> None:
        entries = _load_raw_from_file(history_file)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "provider": provider,
            "results": [_serialize_result(r) for r in results],
            "usage": usage_summary,
        }
        if metadata:
            for key in _ALLOWED_METADATA_KEYS:
                if key in metadata:
                    entry[key] = metadata[key]
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
