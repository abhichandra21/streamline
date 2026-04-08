"""SQLite event store for normalized watch history."""

import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from recommender.ingestion.base import WatchEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL UNIQUE,
    source_path   TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    imported_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS watch_events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    provider                 TEXT NOT NULL,
    title                    TEXT NOT NULL,
    content_type             TEXT NOT NULL,
    series_name              TEXT NOT NULL,
    watched_duration_seconds INTEGER NOT NULL,
    total_duration_seconds   INTEGER,
    timestamp_iso            TEXT NOT NULL,
    profile                  TEXT NOT NULL,
    import_id                INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    source_hash              TEXT NOT NULL UNIQUE
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _compute_source_hash(
    provider: str, profile: str, title: str,
    timestamp_iso: str, watched_duration_seconds: int,
) -> str:
    raw = "\0".join([
        provider, profile, title,
        timestamp_iso, str(watched_duration_seconds),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_db(db_path: str) -> None:
    """Create tables if not present. Create parent directories if needed."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    conn.executescript(_SCHEMA)
    conn.close()


def replace_provider_events(
    db_path: str,
    provider: str,
    events: list[WatchEvent],
    source_path: str,
    source_sha256: str,
) -> int:
    """Replace all events for a provider in a single transaction.

    Returns the number of rows actually persisted (after dedup).
    """
    conn = _connect(db_path)
    try:
        with conn:
            # Delete existing import for this provider (cascade removes events)
            conn.execute("DELETE FROM imports WHERE provider = ?", (provider,))

            # Insert new import row
            cursor = conn.execute(
                "INSERT INTO imports (provider, source_path, source_sha256) VALUES (?, ?, ?)",
                (provider, source_path, source_sha256),
            )
            import_id = cursor.lastrowid

            # Insert events with dedup via INSERT OR IGNORE on source_hash
            for event in events:
                ts_iso = event.timestamp.isoformat(timespec="seconds")
                duration_secs = int(event.watched_duration.total_seconds())
                total_secs = (
                    int(event.total_duration.total_seconds())
                    if event.total_duration is not None
                    else None
                )
                source_hash = _compute_source_hash(
                    provider, event.profile, event.title,
                    ts_iso, duration_secs,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO watch_events "
                    "(provider, title, content_type, series_name, "
                    "watched_duration_seconds, total_duration_seconds, "
                    "timestamp_iso, profile, import_id, source_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (provider, event.title, event.content_type, event.series_name,
                     duration_secs, total_secs, ts_iso, event.profile,
                     import_id, source_hash),
                )

            # Return persisted count
            count = conn.execute(
                "SELECT COUNT(*) FROM watch_events WHERE import_id = ?",
                (import_id,),
            ).fetchone()[0]
            return count
    finally:
        conn.close()


def load_events(db_path: str, provider: str | None = None) -> list[WatchEvent]:
    """Load watch events from SQLite, ordered by timestamp_iso ASC, id ASC.

    Returns [] if the database does not exist or has no events.
    """
    if not Path(db_path).exists():
        return []

    conn = _connect(db_path)
    try:
        query = (
            "SELECT provider, title, content_type, series_name, "
            "watched_duration_seconds, total_duration_seconds, "
            "timestamp_iso, profile FROM watch_events"
        )
        params: tuple = ()
        if provider is not None:
            query += " WHERE provider = ?"
            params = (provider,)
        query += " ORDER BY timestamp_iso ASC, id ASC"

        rows = conn.execute(query, params).fetchall()
        events = []
        for row in rows:
            (prov, title, ct, series, dur_secs, total_secs, ts_iso, profile) = row
            events.append(WatchEvent(
                platform=prov,
                title=title,
                content_type=ct,
                series_name=series,
                watched_duration=timedelta(seconds=dur_secs),
                total_duration=timedelta(seconds=total_secs) if total_secs is not None else None,
                timestamp=datetime.fromisoformat(ts_iso),
                profile=profile,
            ))
        return events
    finally:
        conn.close()


def get_import_info(db_path: str) -> dict[str, dict]:
    """Return per-provider import metadata with event counts."""
    if not Path(db_path).exists():
        return {}

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT i.provider, i.source_path, i.source_sha256, i.imported_at, "
            "COUNT(e.id) as event_count "
            "FROM imports i LEFT JOIN watch_events e ON e.import_id = i.id "
            "GROUP BY i.provider"
        ).fetchall()
        result = {}
        for provider, source_path, source_sha256, imported_at, event_count in rows:
            result[provider] = {
                "source_path": source_path,
                "source_sha256": source_sha256,
                "imported_at": imported_at,
                "event_count": event_count,
            }
        return result
    finally:
        conn.close()
