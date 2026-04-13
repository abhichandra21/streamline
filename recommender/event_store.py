"""SQLite event store for normalized watch history."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("recommender.event_store")

from recommender.ingestion.base import WatchEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    provider             TEXT NOT NULL UNIQUE,
    source_manifest_json TEXT NOT NULL,
    snapshot_sha256      TEXT NOT NULL,
    imported_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _compute_source_hash(
    provider: str, content_type: str, series_name: str, title: str,
    timestamp_iso: str, watched_duration_seconds: int,
) -> str:
    """Compute a dedup hash from event identity fields, excluding profile."""
    raw = "\0".join([
        provider, content_type, series_name, title,
        timestamp_iso, str(watched_duration_seconds),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """Return True if the imports table has the legacy schema (source_path column)."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(imports)").fetchall()
    }
    # Legacy schema has source_path; new schema has source_manifest_json.
    return "source_path" in cols and "source_manifest_json" not in cols


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Drop and recreate imports + watch_events while preserving user-store tables."""
    conn.executescript(f"""
        DROP TABLE IF EXISTS watch_events;
        DROP TABLE IF EXISTS imports;
        {_SCHEMA}
    """)


def init_db(db_path: str) -> None:
    """Create tables if not present. Create parent directories if needed.

    If the legacy schema is detected (source_path column present in imports),
    drop and recreate only imports and watch_events, preserving user-store tables.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        # Check whether the imports table exists at all first.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "imports" in tables and _needs_migration(conn):
            log.warning("Legacy event-store schema detected — migrating. Watch history will be re-ingested on next setup run.")
            _migrate_schema(conn)
        else:
            conn.executescript(_SCHEMA)
    finally:
        conn.close()


def remove_disabled_providers(db_path: str, active_providers: list[str]) -> None:
    """Remove imported events for providers that are no longer configured."""
    if not active_providers:
        conn = _connect(db_path)
        try:
            with conn:
                conn.execute("DELETE FROM imports")
        finally:
            conn.close()
        return

    placeholders = ",".join("?" * len(active_providers))
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                f"DELETE FROM imports WHERE provider NOT IN ({placeholders})",
                active_providers
            )
    finally:
        conn.close()


def replace_provider_events(
    db_path: str,
    provider: str,
    events: list[WatchEvent],
    source_manifest: list[dict],
    snapshot_sha256: str,
) -> tuple[int, int]:
    """Replace all events for a provider in a single transaction.

    Args:
        db_path: Path to the SQLite database.
        provider: Provider name (e.g. "netflix").
        events: Normalized watch events to persist.
        source_manifest: List of {"path": str, "sha256": str} dicts describing
            the source files that make up this snapshot.
        snapshot_sha256: SHA-256 of the normalized manifest, used for change detection.

    Returns:
        (persisted_count, total_raw_count) tuple. Difference is duplicates removed.
    """
    manifest_json = json.dumps(source_manifest, separators=(",", ":"))
    total_raw = len(events)

    conn = _connect(db_path)
    try:
        with conn:
            # Delete existing import for this provider (cascade removes events).
            conn.execute("DELETE FROM imports WHERE provider = ?", (provider,))

            # Insert new import row.
            cursor = conn.execute(
                "INSERT INTO imports (provider, source_manifest_json, snapshot_sha256) "
                "VALUES (?, ?, ?)",
                (provider, manifest_json, snapshot_sha256),
            )
            import_id = cursor.lastrowid

            # In-memory set to catch intra-batch duplicates before hitting the DB.
            # First-seen profile wins for duplicate events; profile is not part of the identity key.
            seen_hashes: set[str] = set()

            for event in events:
                ts_iso = event.timestamp.isoformat(timespec="seconds")
                duration_secs = int(event.watched_duration.total_seconds())
                total_secs = (
                    int(event.total_duration.total_seconds())
                    if event.total_duration is not None
                    else None
                )
                source_hash = _compute_source_hash(
                    provider, event.content_type, event.series_name,
                    event.title, ts_iso, duration_secs,
                )
                if source_hash in seen_hashes:
                    continue
                seen_hashes.add(source_hash)

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

            persisted = conn.execute(
                "SELECT COUNT(*) FROM watch_events WHERE import_id = ?",
                (import_id,),
            ).fetchone()[0]
            return (persisted, total_raw)
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
            "SELECT i.provider, i.source_manifest_json, i.snapshot_sha256, i.imported_at, "
            "COUNT(e.id) as event_count "
            "FROM imports i LEFT JOIN watch_events e ON e.import_id = i.id "
            "GROUP BY i.provider"
        ).fetchall()
        result = {}
        for provider, manifest_json, snapshot_sha256, imported_at, event_count in rows:
            result[provider] = {
                "source_manifest": json.loads(manifest_json),
                "snapshot_sha256": snapshot_sha256,
                "imported_at": imported_at,
                "event_count": event_count,
            }
        return result
    finally:
        conn.close()
