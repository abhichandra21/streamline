"""SQLite user store for watchlist, ratings, and manual archive entries."""

import json as _json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("recommender.user_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_titles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    content_type     TEXT NOT NULL CHECK (content_type IN ('tv', 'movie')),
    tmdb_id          INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('watchlist', 'dismissed')),
    saved_at         TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_ratings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    content_type     TEXT NOT NULL CHECK (content_type IN ('tv', 'movie')),
    tmdb_id          INTEGER,
    rating           TEXT NOT NULL CHECK (rating IN ('liked', 'disliked')),
    rated_at         TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_archive_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    content_type     TEXT NOT NULL CHECK (content_type IN ('tv', 'movie')),
    tmdb_id          INTEGER,
    watched_at       TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('web', 'cli', 'feedback_migration'))
);

CREATE TABLE IF NOT EXISTS user_store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS saved_titles_tmdb_unique
ON saved_titles (content_type, tmdb_id)
WHERE tmdb_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS saved_titles_title_unique
ON saved_titles (content_type, normalized_title)
WHERE tmdb_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS title_ratings_tmdb_unique
ON title_ratings (content_type, tmdb_id)
WHERE tmdb_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS title_ratings_title_unique
ON title_ratings (content_type, normalized_title)
WHERE tmdb_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS manual_archive_tmdb_unique
ON manual_archive_entries (content_type, tmdb_id)
WHERE tmdb_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS manual_archive_title_unique
ON manual_archive_entries (content_type, normalized_title)
WHERE tmdb_id IS NULL;
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _normalize(title: str) -> str:
    """Lowercase, strip parenthetical suffixes. Same rule as watch_index._normalize."""
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reconcile_identity(conn: sqlite3.Connection, table: str, title: str,
                        content_type: str, tmdb_id: int | None) -> None:
    """Promote a NULL-tmdb row to a concrete tmdb_id before upsert."""
    if tmdb_id is None:
        return
    norm = _normalize(title)
    row = conn.execute(
        f"SELECT id, tmdb_id FROM {table} "
        "WHERE normalized_title = ? AND content_type = ? "
        "ORDER BY tmdb_id IS NOT NULL DESC, id ASC LIMIT 1",
        (norm, content_type),
    ).fetchone()
    if row and row[1] is None:
        conn.execute(
            f"UPDATE {table} SET title = ?, normalized_title = ?, tmdb_id = ? WHERE id = ?",
            (title, norm, tmdb_id, row[0]),
        )


def resolve_rating_content_type(db_path: str, title: str,
                                tmdb_id: int | None = None) -> str:
    """Infer legacy rating/addition content_type or raise a clear error."""
    from recommender.event_store import load_events

    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        if tmdb_id is not None:
            for table in ("saved_titles", "title_ratings", "manual_archive_entries"):
                row = conn.execute(
                    f"SELECT content_type FROM {table} WHERE tmdb_id = ? LIMIT 1",
                    (tmdb_id,),
                ).fetchone()
                if row:
                    return row[0]

        for table in ("saved_titles", "title_ratings", "manual_archive_entries"):
            rows = conn.execute(
                f"SELECT DISTINCT content_type FROM {table} WHERE normalized_title = ?",
                (norm,),
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]

        try:
            events = load_events(db_path)
        except sqlite3.OperationalError:
            events = []
        event_matches = {
            event.content_type
            for event in events
            if _normalize(event.series_name if event.content_type == "tv" else event.title) == norm
        }
        if len(event_matches) == 1:
            return event_matches.pop()
    finally:
        conn.close()

    raise ValueError(
        f"Could not infer content type for {title!r}. "
        "Use an explicit tv/movie type or migrate matching watch history first."
    )


def init_db(db_path: str) -> None:
    """Create tables and indexes if not present."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executescript(_INDEXES)
    finally:
        conn.close()


def save_title(db_path: str, title: str, content_type: str,
               tmdb_id: int | None = None, status: str = "watchlist") -> None:
    """Upsert a title into saved_titles. If dismissed, re-saving sets status to watchlist."""
    now = _now_iso()
    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        with conn:
            _reconcile_identity(conn, "saved_titles", title, content_type, tmdb_id)
            if tmdb_id is not None:
                conn.execute(
                    "INSERT INTO saved_titles "
                    "(title, normalized_title, content_type, tmdb_id, status, saved_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                    "DO UPDATE SET title=excluded.title, normalized_title=excluded.normalized_title, "
                    "status=excluded.status, updated_at=excluded.updated_at",
                    (title, norm, content_type, tmdb_id, status, now, now),
                )
            else:
                conn.execute(
                    "INSERT INTO saved_titles "
                    "(title, normalized_title, content_type, tmdb_id, status, saved_at, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                    "DO UPDATE SET title=excluded.title, status=excluded.status, "
                    "updated_at=excluded.updated_at",
                    (title, norm, content_type, status, now, now),
                )
    finally:
        conn.close()


def dismiss_title(db_path: str, title: str, content_type: str,
                  tmdb_id: int | None = None) -> None:
    """Upsert a title as dismissed."""
    save_title(db_path, title, content_type, tmdb_id=tmdb_id, status="dismissed")


def remove_saved_title(db_path: str, title: str, content_type: str) -> None:
    """Delete a saved_titles row (watchlist only)."""
    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "DELETE FROM saved_titles WHERE normalized_title = ? AND content_type = ? "
                "AND status = 'watchlist'",
                (norm, content_type),
            )
    finally:
        conn.close()


def list_saved_titles(db_path: str, status: str | None = None) -> list[dict]:
    """Return saved titles, optionally filtered by status."""
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        if status:
            rows = conn.execute(
                "SELECT title, normalized_title, content_type, tmdb_id, status, saved_at, updated_at "
                "FROM saved_titles WHERE status = ? ORDER BY saved_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT title, normalized_title, content_type, tmdb_id, status, saved_at, updated_at "
                "FROM saved_titles ORDER BY saved_at DESC",
            ).fetchall()
        return [
            {
                "title": r[0], "normalized_title": r[1], "content_type": r[2],
                "tmdb_id": r[3], "status": r[4], "saved_at": r[5], "updated_at": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


LIKED_MULTIPLIER = 1.3
DISLIKED_MULTIPLIER = 0.5


def rate_title(db_path: str, title: str, content_type: str | None,
               rating: str, tmdb_id: int | None = None) -> None:
    """Upsert a rating. rating='clear' removes it."""
    content_type = content_type or resolve_rating_content_type(db_path, title, tmdb_id=tmdb_id)
    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        with conn:
            _reconcile_identity(conn, "title_ratings", title, content_type, tmdb_id)
            if rating == "clear":
                if tmdb_id is not None:
                    conn.execute(
                        "DELETE FROM title_ratings "
                        "WHERE content_type = ? AND tmdb_id = ?",
                        (content_type, tmdb_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM title_ratings "
                        "WHERE content_type = ? AND normalized_title = ? AND tmdb_id IS NULL",
                        (content_type, norm),
                    )
                return

            now = _now_iso()
            if tmdb_id is not None:
                conn.execute(
                    "INSERT INTO title_ratings "
                    "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                    "DO UPDATE SET title=excluded.title, normalized_title=excluded.normalized_title, "
                    "rating=excluded.rating, updated_at=excluded.updated_at",
                    (title, norm, content_type, tmdb_id, rating, now, now),
                )
            else:
                conn.execute(
                    "INSERT INTO title_ratings "
                    "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                    "DO UPDATE SET title=excluded.title, rating=excluded.rating, "
                    "updated_at=excluded.updated_at",
                    (title, norm, content_type, rating, now, now),
                )
    finally:
        conn.close()


def remove_rating(db_path: str, title: str, content_type: str) -> None:
    """Remove a rating entirely."""
    rate_title(db_path, title, content_type, "clear")


def load_ratings(db_path: str) -> list[dict]:
    """Return all ratings."""
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT title, normalized_title, content_type, tmdb_id, rating, rated_at "
            "FROM title_ratings ORDER BY rated_at DESC",
        ).fetchall()
        return [
            {
                "title": r[0], "normalized_title": r[1], "content_type": r[2],
                "tmdb_id": r[3], "rating": r[4], "rated_at": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_disliked_titles(db_path: str) -> list[str]:
    """Return titles rated as disliked."""
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT title FROM title_ratings WHERE rating = 'disliked'",
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def apply_rating_multipliers(scores: dict[str, float],
                             ratings: list[dict]) -> dict[str, float]:
    """Apply liked/disliked multipliers to engagement scores.

    Liked titles get a 1.3x boost (capped at 1.0); disliked get 0.5x penalty.
    """
    modified = dict(scores)
    for entry in ratings:
        title = entry["title"]
        rating = entry["rating"]
        if title in modified:
            if rating == "liked":
                modified[title] = min(1.0, modified[title] * LIKED_MULTIPLIER)
            elif rating == "disliked":
                modified[title] = modified[title] * DISLIKED_MULTIPLIER
    return modified


def add_to_archive(db_path: str, title: str, content_type: str,
                   tmdb_id: int | None = None, source: str = "web") -> None:
    """Upsert a manual archive entry."""
    now = _now_iso()
    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        with conn:
            _reconcile_identity(conn, "manual_archive_entries", title, content_type, tmdb_id)
            if tmdb_id is not None:
                conn.execute(
                    "INSERT INTO manual_archive_entries "
                    "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                    "DO UPDATE SET title=excluded.title, normalized_title=excluded.normalized_title, "
                    "watched_at=excluded.watched_at, source=excluded.source",
                    (title, norm, content_type, tmdb_id, now, source),
                )
            else:
                conn.execute(
                    "INSERT INTO manual_archive_entries "
                    "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                    "VALUES (?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                    "DO UPDATE SET title=excluded.title, watched_at=excluded.watched_at, "
                    "source=excluded.source",
                    (title, norm, content_type, now, source),
                )
    finally:
        conn.close()


def mark_watched_from_watchlist(db_path: str, title: str, content_type: str,
                                rating: str | None = None,
                                tmdb_id: int | None = None) -> None:
    """Atomic: remove from saved_titles, add to manual_archive, optionally rate.

    If any step fails, the entire transaction rolls back.
    """
    now = _now_iso()
    norm = _normalize(title)
    conn = _connect(db_path)
    try:
        with conn:
            # Delete from saved_titles
            if tmdb_id is not None:
                conn.execute(
                    "DELETE FROM saved_titles WHERE content_type = ? AND tmdb_id = ?",
                    (content_type, tmdb_id),
                )
            else:
                conn.execute(
                    "DELETE FROM saved_titles "
                    "WHERE content_type = ? AND normalized_title = ? AND tmdb_id IS NULL",
                    (content_type, norm),
                )

            # Upsert manual archive entry
            _reconcile_identity(conn, "manual_archive_entries", title, content_type, tmdb_id)
            if tmdb_id is not None:
                conn.execute(
                    "INSERT INTO manual_archive_entries "
                    "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                    "VALUES (?, ?, ?, ?, ?, 'web') "
                    "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                    "DO UPDATE SET title=excluded.title, normalized_title=excluded.normalized_title, "
                    "watched_at=excluded.watched_at, source=excluded.source",
                    (title, norm, content_type, tmdb_id, now),
                )
            else:
                conn.execute(
                    "INSERT INTO manual_archive_entries "
                    "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                    "VALUES (?, ?, ?, NULL, ?, 'web') "
                    "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                    "DO UPDATE SET title=excluded.title, watched_at=excluded.watched_at, "
                    "source=excluded.source",
                    (title, norm, content_type, now),
                )

            # Optional rating
            if rating and rating != "clear":
                _reconcile_identity(conn, "title_ratings", title, content_type, tmdb_id)
                if tmdb_id is not None:
                    conn.execute(
                        "INSERT INTO title_ratings "
                        "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                        "DO UPDATE SET title=excluded.title, normalized_title=excluded.normalized_title, "
                        "rating=excluded.rating, updated_at=excluded.updated_at",
                        (title, norm, content_type, tmdb_id, rating, now, now),
                    )
                else:
                    conn.execute(
                        "INSERT INTO title_ratings "
                        "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?) "
                        "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                        "DO UPDATE SET title=excluded.title, rating=excluded.rating, "
                        "updated_at=excluded.updated_at",
                        (title, norm, content_type, rating, now, now),
                    )
    finally:
        conn.close()


def list_manual_archive(db_path: str) -> list[dict]:
    """Return all manual archive entries."""
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT title, normalized_title, content_type, tmdb_id, watched_at, source "
            "FROM manual_archive_entries ORDER BY watched_at DESC",
        ).fetchall()
        return [
            {
                "title": r[0], "normalized_title": r[1], "content_type": r[2],
                "tmdb_id": r[3], "watched_at": r[4], "source": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def ensure_user_store(db_path: str, feedback_path: str) -> None:
    """Create tables/indexes and run one-time migration from feedback.json if needed."""
    init_db(db_path)

    fp = Path(feedback_path)
    if not fp.exists():
        return

    conn = _connect(db_path)
    try:
        # Check for migration marker
        marker = conn.execute(
            "SELECT value FROM user_store_meta WHERE key = 'feedback_migrated'"
        ).fetchone()

        if marker:
            # Marker exists — just retry the rename if JSON still present
            fp.rename(str(fp) + ".migrated")
            log.info("Renamed leftover %s after prior migration", feedback_path)
            return

        # Check for existing rows (conflict guard)
        row_count = 0
        for table in ("saved_titles", "title_ratings", "manual_archive_entries"):
            row_count += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        if row_count > 0:
            raise RuntimeError(
                f"User-store tables already contain {row_count} rows but "
                f"{feedback_path} has not been migrated. Cannot safely proceed. "
                "Remove the JSON file manually if the data has already been migrated."
            )

        # Load and import feedback.json
        data = _json.loads(fp.read_text())

        with conn:
            for entry in data.get("ratings", []):
                title = entry["title"]
                rating = entry.get("rating", "liked")
                tmdb_id = entry.get("tmdb_id")
                ct = entry.get("content_type") or resolve_rating_content_type(db_path, title, tmdb_id=tmdb_id)
                ts = entry.get("timestamp", _now_iso())
                norm = _normalize(title)
                if tmdb_id is not None:
                    conn.execute(
                        "INSERT INTO title_ratings "
                        "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                        "DO UPDATE SET rating=excluded.rating, updated_at=excluded.updated_at",
                        (title, norm, ct, tmdb_id, rating, ts, ts),
                    )
                else:
                    conn.execute(
                        "INSERT INTO title_ratings "
                        "(title, normalized_title, content_type, tmdb_id, rating, rated_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?) "
                        "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                        "DO UPDATE SET rating=excluded.rating, updated_at=excluded.updated_at",
                        (title, norm, ct, rating, ts, ts),
                    )

            for entry in data.get("additions", []):
                title = entry["title"]
                tmdb_id = entry.get("tmdb_id")
                ct = entry.get("content_type") or resolve_rating_content_type(db_path, title, tmdb_id=tmdb_id)
                ts = entry.get("timestamp", _now_iso())
                norm = _normalize(title)
                if tmdb_id is not None:
                    conn.execute(
                        "INSERT INTO manual_archive_entries "
                        "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                        "VALUES (?, ?, ?, ?, ?, 'feedback_migration') "
                        "ON CONFLICT (content_type, tmdb_id) WHERE tmdb_id IS NOT NULL "
                        "DO UPDATE SET watched_at=excluded.watched_at, source=excluded.source",
                        (title, norm, ct, tmdb_id, ts),
                    )
                else:
                    conn.execute(
                        "INSERT INTO manual_archive_entries "
                        "(title, normalized_title, content_type, tmdb_id, watched_at, source) "
                        "VALUES (?, ?, ?, NULL, ?, 'feedback_migration') "
                        "ON CONFLICT (content_type, normalized_title) WHERE tmdb_id IS NULL "
                        "DO UPDATE SET watched_at=excluded.watched_at, source=excluded.source",
                        (title, norm, ct, ts),
                    )

            # Write migration marker inside the transaction
            conn.execute(
                "INSERT OR REPLACE INTO user_store_meta (key, value) "
                "VALUES ('feedback_migrated', '1')"
            )

        # Rename after successful transaction
        fp.rename(str(fp) + ".migrated")
        log.info("Migrated %s to SQLite and renamed to %s.migrated", feedback_path, feedback_path)

    finally:
        conn.close()
