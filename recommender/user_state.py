"""Snapshot of user-managed state for query filtering and UI rendering."""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def _normalize(title: str) -> str:
    """Same normalization as watch_index and user_store."""
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


@dataclass
class UserStateIndex:
    """Immutable snapshot. Build a new instance to see mutations."""

    # manual_archive: TMDB IDs and (normalized_title, content_type) tuples
    _archive_tmdb_ids: set[int] = field(default_factory=set)
    _archive_titles: set[tuple[str, str]] = field(default_factory=set)

    # dismissed: same dual-key pattern
    _dismissed_tmdb_ids: set[int] = field(default_factory=set)
    _dismissed_titles: set[tuple[str, str]] = field(default_factory=set)

    # watchlist: same dual-key pattern
    _watchlist_tmdb_ids: set[int] = field(default_factory=set)
    _watchlist_titles: set[tuple[str, str]] = field(default_factory=set)

    # ratings: keyed by tmdb_id or (normalized_title, content_type)
    _ratings_by_tmdb: dict[int, str] = field(default_factory=dict)
    _ratings_by_title: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, db_path: str) -> "UserStateIndex":
        """Build a snapshot from the current SQLite state."""
        idx = cls()
        if not Path(db_path).exists():
            return idx

        conn = sqlite3.connect(db_path)
        try:
            # Manual archive
            for row in conn.execute(
                "SELECT tmdb_id, normalized_title, content_type FROM manual_archive_entries"
            ).fetchall():
                tmdb_id, norm, ct = row
                if tmdb_id is not None:
                    idx._archive_tmdb_ids.add(tmdb_id)
                idx._archive_titles.add((norm, ct))

            # Saved titles (watchlist + dismissed)
            for row in conn.execute(
                "SELECT tmdb_id, normalized_title, content_type, status FROM saved_titles"
            ).fetchall():
                tmdb_id, norm, ct, status = row
                if status == "dismissed":
                    if tmdb_id is not None:
                        idx._dismissed_tmdb_ids.add(tmdb_id)
                    idx._dismissed_titles.add((norm, ct))
                elif status == "watchlist":
                    if tmdb_id is not None:
                        idx._watchlist_tmdb_ids.add(tmdb_id)
                    idx._watchlist_titles.add((norm, ct))

            # Ratings
            for row in conn.execute(
                "SELECT tmdb_id, normalized_title, content_type, rating FROM title_ratings"
            ).fetchall():
                tmdb_id, norm, ct, rating = row
                if tmdb_id is not None:
                    idx._ratings_by_tmdb[tmdb_id] = rating
                idx._ratings_by_title[(norm, ct)] = rating

        finally:
            conn.close()

        return idx

    def _match_tmdb_first(self, meta, tmdb_set: set, title_set: set) -> bool:
        if meta.tmdb_id is not None:
            return meta.tmdb_id in tmdb_set
        return (_normalize(meta.title), meta.content_type) in title_set

    def is_manually_watched(self, meta) -> bool:
        return self._match_tmdb_first(meta, self._archive_tmdb_ids, self._archive_titles)

    def is_dismissed(self, meta) -> bool:
        return self._match_tmdb_first(meta, self._dismissed_tmdb_ids, self._dismissed_titles)

    def is_in_watchlist(self, meta) -> bool:
        return self._match_tmdb_first(meta, self._watchlist_tmdb_ids, self._watchlist_titles)

    def has_rating(self, meta) -> bool:
        return self.get_rating(meta) is not None

    def get_rating(self, meta) -> str | None:
        if meta.tmdb_id is not None:
            return self._ratings_by_tmdb.get(meta.tmdb_id)
        key = (_normalize(meta.title), meta.content_type)
        return self._ratings_by_title.get(key)
