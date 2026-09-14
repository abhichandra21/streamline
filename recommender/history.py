"""Query history storage.

Keeps the last MAX_ENTRIES queries with their results, timestamps, and usage
stats in the configured event database. One row per entry; the whole entry is
stored as JSON so optional and future metadata survive round-trips untouched.

The legacy JSON file (query_history.json) is imported once on first use and
then kept as a backup — it is no longer read or written.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("recommender.history")

LEGACY_HISTORY_PATH = Path(config.ENRICHMENT_CACHE_DIR).parent / "query_history.json"
MAX_ENTRIES = 100
_MIGRATION_KEY = "json_history_migrated"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    entry     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS query_history_timestamp ON query_history (timestamp);

CREATE TABLE IF NOT EXISTS query_history_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class MigrationFailed(Exception):
    """The legacy JSON history could not be imported. Nothing was changed."""


def _db_path() -> str:
    return config.EVENT_DB_PATH


def _connect(db_path: str, *, create: bool = True) -> sqlite3.Connection:
    """Open the store. ``create=False`` opens read-only, so a probe cannot
    bring a database into being or change its journal mode."""
    if not create:
        conn = sqlite3.connect(
            f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        return conn

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _legacy_path() -> Path:
    return Path(LEGACY_HISTORY_PATH)


def _trim(conn: sqlite3.Connection) -> None:
    """Keep only the newest MAX_ENTRIES rows. Caller owns the transaction."""
    conn.execute(
        "DELETE FROM query_history WHERE id NOT IN ("
        "  SELECT id FROM query_history ORDER BY id DESC LIMIT ?"
        ")",
        (MAX_ENTRIES,),
    )


def _marker_present(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM query_history_meta WHERE key = ?", (_MIGRATION_KEY,)
    ).fetchone() is not None


def _migration_done(db_path: str) -> bool:
    """True if a previous run already imported the legacy file.

    Read on its own connection, before the store is opened for writing, and
    tolerant of a database that has no history tables yet.
    """
    if not Path(db_path).exists():
        return False

    conn = _connect(db_path, create=False)
    try:
        if not conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='query_history_meta'"
        ).fetchone():
            return False
        return _marker_present(conn)
    finally:
        conn.close()


def _legacy_entries_to_import(db_path: str) -> list[dict] | None:
    """Parse and validate the legacy file if its import is still pending.

    Returns None when there is nothing to import. Runs before the store is
    opened, so a malformed file cannot leave behind so much as an empty table:
    the contract is that both the file and the database stay as they were.
    """
    legacy = _legacy_path()
    if not legacy.exists():
        return None
    if _migration_done(db_path):
        # A file that turns up after migration completed is a backup, not a
        # source. Importing it would merge two histories. To import it on
        # purpose, delete the marker row from query_history_meta first.
        log.debug("Ignoring %s: query history migration already completed", legacy)
        return None

    try:
        raw_text = legacy.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Another process imported it and moved it aside between the two checks.
        return None
    except OSError as exc:
        raise MigrationFailed(
            f"Cannot read query history from {legacy}: {exc}. "
            "The file was left untouched — fix or move it aside to continue."
        ) from exc

    if not raw_text.strip():
        # An empty file has nothing to recover: the old store wrote one
        # whenever delete() ran against a store with no file yet, and a write
        # interrupted mid-truncate leaves the same. Same as a literal "[]" —
        # import nothing, mark it done, keep the file as a backup.
        return []

    try:
        entries = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MigrationFailed(
            f"Cannot read query history from {legacy}: {exc}. "
            "The file was left untouched — fix or move it aside to continue."
        ) from exc

    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise MigrationFailed(
            f"{legacy} is not a list of history entries. The file was left "
            "untouched — fix or move it aside to continue."
        )
    # timestamp is the one value the import binds as a column, so its type has
    # to hold. Everything else is stored as JSON exactly as it arrived.
    if any(not isinstance(e.get("timestamp", ""), str) for e in entries):
        raise MigrationFailed(
            f"{legacy} has an entry whose timestamp is not a string. The file "
            "was left untouched — fix or move it aside to continue."
        )
    return entries


def _mark_migration_complete(conn: sqlite3.Connection) -> None:
    """Record that the store is SQLite-native, with no legacy file to import.

    Without this, a first use that finds no query_history.json leaves the
    marker unset, and a JSON file appearing later — a restored backup, a copy
    from another machine — would be imported and merged into history that the
    store has accumulated since. Serialized the same way as the import.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _marker_present(conn):
            conn.rollback()
            return
        conn.execute(
            "INSERT OR REPLACE INTO query_history_meta (key, value) VALUES (?, '1')",
            (_MIGRATION_KEY,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _import_legacy(conn: sqlite3.Connection, entries: list[dict]) -> None:
    """Import already-validated legacy entries, then keep the file as a backup.

    The CLI and the web UI can both reach a fresh store at the same time, so
    the marker is re-read under the migration write lock: whoever gets the
    lock imports, the others see the marker and leave. Parsing happened before
    the lock was taken so a slow read cannot block another process's writes.
    """
    legacy = _legacy_path()

    # BEGIN IMMEDIATE takes the write lock before the marker is re-read, so the
    # check and the import cannot be interleaved by another process.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _marker_present(conn):
            conn.rollback()
            return
        # Oldest first, so row ids keep the file's order.
        for entry in entries:
            conn.execute(
                "INSERT INTO query_history (timestamp, entry) VALUES (?, ?)",
                (entry.get("timestamp", ""), json.dumps(entry)),
            )
        _trim(conn)
        conn.execute(
            "INSERT OR REPLACE INTO query_history_meta (key, value) VALUES (?, '1')",
            (_MIGRATION_KEY,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    backup = legacy.with_suffix(legacy.suffix + ".migrated")
    try:
        legacy.rename(backup)
    except FileNotFoundError:
        log.info("Legacy query history was already moved aside after import")
    else:
        log.info("Imported %d query history entries from %s, kept as %s",
                 len(entries), legacy, backup)


def _open(db_path: str | None) -> sqlite3.Connection:
    """Open the history store, creating tables and importing legacy JSON once."""
    path = db_path or _db_path()
    # Validation first: a malformed legacy file raises here, before the store
    # is created or its schema written.
    legacy_entries = _legacy_entries_to_import(path)
    conn = _connect(path)
    try:
        with conn:
            conn.executescript(_SCHEMA)
        if legacy_entries is not None:
            _import_legacy(conn, legacy_entries)
        elif not _marker_present(conn):
            _mark_migration_complete(conn)
    except Exception:
        conn.close()
        raise
    return conn


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
    db_path: str | None = None,
) -> None:
    """Append a query + results to history, capped at MAX_ENTRIES.

    ``results`` may be enriched dicts (from web.py) or raw Recommendation objects.
    ``metadata`` carries optional structured fields (e.g. wizard source/label/intent)
    merged into the entry under an explicit allowlist. ``query`` is always kept for
    backward compatibility with old entries.
    """
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

    conn = _open(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO query_history (timestamp, entry) VALUES (?, ?)",
                (entry["timestamp"], json.dumps(entry)),
            )
            _trim(conn)
    finally:
        conn.close()


def delete(timestamp: str, db_path: str | None = None) -> bool:
    """Delete a history entry by its timestamp. Returns True if found and removed."""
    conn = _open(db_path)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM query_history WHERE timestamp = ?", (timestamp,)
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def load(limit: int | None = None, db_path: str | None = None) -> list[dict]:
    """Load history entries, most recent first."""
    conn = _open(db_path)
    try:
        sql = "SELECT entry FROM query_history ORDER BY id DESC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    entries = []
    for row in rows:
        try:
            entries.append(json.loads(row["entry"]))
        except json.JSONDecodeError:
            log.warning("Skipping unreadable query history row")
    return entries
