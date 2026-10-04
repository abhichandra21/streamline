"""Plex: save plays from Plex webhooks and bring Plex ratings into Streamline.

Plex calls POST /plex/webhook on every playback event. Only media.scrobble,
which Plex sends once when a play counts as watched, is saved, as a 'plex'
watch event. Plex plays are the only copy: there is no export to rebuild them
from, so setup never deletes them (event_store.PRESERVED_PROVIDERS).

Rating in Plex sends no webhook, so after each saved play the rated movies and
shows are read from the Plex API and applied newer-wins. `./recommend plex
ratings` runs the same sync by hand. Nothing is ever written to Plex.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

import config
from recommender import event_store, user_store
from recommender.ingestion.base import WatchEvent, detect_language_hint

log = logging.getLogger("recommender.plex")

PROVIDER = "plex"
SCROBBLE_EVENT = "media.scrobble"
REQUEST_TIMEOUT_SECONDS = 5

# Install-local: the time the last complete rating sync started (epoch seconds).
RATINGS_MARK_KEY = "plex_ratings_synced_through"

# Plex stores user ratings on a 0-10 scale (half stars on a 5-star display).
_BAND_MORE_FROM = 8.0
_BAND_NEUTRAL_FROM = 6.0

_RATED_TYPES = {"movie": "movie", "show": "tv"}


class PlexError(Exception):
    """A call to the Plex server failed or returned something unusable."""


@dataclass
class RatingChange:
    title: str
    content_type: str
    old: str | None
    new: str


class PlexClient:
    """Read-only client for the few Plex server endpoints Streamline uses."""

    def __init__(self, base_url: str, token: str, session=None):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session = session or requests.Session()
        # Keyed by the show's ratingKey. Only definite answers are cached, so
        # a failed lookup is retried on the next episode.
        self._show_tmdb_ids: dict[str, int | None] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self._session.get(
                self._base_url + path,
                headers={"X-Plex-Token": self._token, "Accept": "application/json"},
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()["MediaContainer"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            # The token travels in a header, so the message never carries it.
            raise PlexError(f"Plex request to {path} failed: {exc}") from exc

    def show_tmdb_id(self, rating_key: str) -> int | None:
        """Return the TMDB ID of the show with this ratingKey, or None if Plex has none."""
        if rating_key not in self._show_tmdb_ids:
            items = self._get(f"/library/metadata/{rating_key}").get("Metadata") or [{}]
            self._show_tmdb_ids[rating_key] = tmdb_id_from_guids(items[0].get("Guid"))
        return self._show_tmdb_ids[rating_key]

    def rated_items(self) -> list[dict]:
        """Return every rated item in the movie and show libraries."""
        sections = self._get("/library/sections").get("Directory", [])
        items: list[dict] = []
        for section in sections:
            if section.get("type") not in _RATED_TYPES:
                continue
            container = self._get(
                f"/library/sections/{section['key']}/all",
                params={"userRating>>": "0.1", "includeGuids": "1"},
            )
            items.extend(container.get("Metadata", []))
        return items


def client_from_config() -> PlexClient | None:
    """Build a client from PLEX_URL and PLEX_TOKEN, or None if either is unset."""
    if not config.PLEX_URL or not config.PLEX_TOKEN:
        return None
    return PlexClient(config.PLEX_URL, config.PLEX_TOKEN)


def tmdb_id_from_guids(guids: list[dict] | None) -> int | None:
    for guid in guids or []:
        value = guid.get("id", "")
        if value.startswith("tmdb://"):
            try:
                return int(value.removeprefix("tmdb://"))
            except ValueError:
                return None
    return None


def rating_band(user_rating: float | None) -> str | None:
    """Map a Plex 0-10 rating onto Streamline's three ratings; None if unrated."""
    if not user_rating:
        return None
    if user_rating >= _BAND_MORE_FROM:
        return user_store.RATING_MORE
    if user_rating >= _BAND_NEUTRAL_FROM:
        return user_store.RATING_NEUTRAL
    return user_store.RATING_LESS


def _show_tmdb_id(client: PlexClient | None, rating_key: str | None) -> int | None:
    if client is None or not rating_key:
        return None
    try:
        return client.show_tmdb_id(str(rating_key))
    except PlexError as exc:
        log.warning("Plex show lookup failed, saving the play without a TMDB ID: %s", exc)
        return None


def event_from_payload(payload: dict, client: PlexClient | None) -> WatchEvent | None:
    """Turn a webhook payload into a watch event; None for anything but a video scrobble."""
    if payload.get("event") != SCROBBLE_EVENT:
        return None
    meta = payload.get("Metadata") or {}
    kind = meta.get("type")
    if kind not in ("movie", "episode"):
        return None

    viewed_at = meta.get("lastViewedAt")
    timestamp = (
        datetime.fromtimestamp(viewed_at, tz=timezone.utc).replace(tzinfo=None)
        if viewed_at else datetime.now(timezone.utc).replace(tzinfo=None)
    )
    if meta.get("duration"):
        duration = timedelta(milliseconds=meta["duration"])
    elif kind == "movie":
        duration = timedelta(minutes=config.MANUAL_MOVIE_DURATION_MINUTES)
    else:
        duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES)
    profile = (payload.get("Account") or {}).get("title", "")

    if kind == "movie":
        title = meta.get("title", "")
        return WatchEvent(
            platform=PROVIDER,
            title=title,
            content_type="movie",
            series_name=title,
            watched_duration=duration,
            total_duration=duration,
            timestamp=timestamp,
            profile=profile,
            release_year_hint=meta.get("year"),
            language_hint=detect_language_hint(title),
            tmdb_id_hint=tmdb_id_from_guids(meta.get("Guid")),
        )

    show = meta.get("grandparentTitle", "")
    # Netflix's episode format, so classify_title reads Plex episodes the same way.
    title = (f"{show}: Season {meta.get('parentIndex')}: "
             f"{meta.get('title', '')} (Episode {meta.get('index')})")
    return WatchEvent(
        platform=PROVIDER,
        title=title,
        content_type="tv",
        series_name=show,
        watched_duration=duration,
        total_duration=duration,
        timestamp=timestamp,
        profile=profile,
        language_hint=detect_language_hint(show),
        tmdb_id_hint=_show_tmdb_id(client, meta.get("grandparentRatingKey")),
    )


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def sync_ratings(db_path: str, client: PlexClient,
                 now: Callable[[], float] = time.time) -> list[RatingChange]:
    """Apply Plex ratings changed since the last sync, newer-wins. Returns what changed.

    Raises PlexError if Plex fails; the mark then stays put so the next run retries.
    """
    user_store.ensure_user_store(db_path, config.FEEDBACK_PATH)
    scan_start = int(now())
    mark_value = user_store.get_meta(db_path, RATINGS_MARK_KEY)
    mark = int(mark_value) if mark_value else None

    items = client.rated_items()

    ratings = user_store.load_ratings(db_path)
    by_tmdb = {(r["content_type"], r["tmdb_id"]): r for r in ratings if r["tmdb_id"]}
    by_title = {(r["content_type"], r["normalized_title"]): r for r in ratings}

    changes: list[RatingChange] = []
    for item in items:
        content_type = _RATED_TYPES.get(item.get("type"))
        band = rating_band(item.get("userRating"))
        if content_type is None or band is None:
            continue
        # A rating with no rated time counts as older than anything in Streamline.
        rated_at = item.get("lastRatedAt") or 0
        if mark is not None and rated_at <= mark:
            continue

        title = item.get("title", "")
        tmdb_id = tmdb_id_from_guids(item.get("Guid"))
        existing = (by_tmdb.get((content_type, tmdb_id)) if tmdb_id else None) or \
            by_title.get((content_type, user_store._normalize(title)))
        if existing and (existing["rating"] == band or _epoch(existing["updated_at"]) >= rated_at):
            continue

        user_store.rate_title(db_path, title, content_type, band, tmdb_id=tmdb_id)
        change = RatingChange(title, content_type, existing["rating"] if existing else None, band)
        log.info("Plex rating applied: %s (%s) %s -> %s",
                 title, content_type, change.old or "unrated", band)
        changes.append(change)

    # The scan's start, not the newest rating seen: a rating changed during
    # the scan in an already-read section is then read again next time.
    user_store.set_meta(db_path, RATINGS_MARK_KEY, str(scan_start))
    return changes


def handle_webhook(payload: dict, db_path: str, client: PlexClient | None) -> dict:
    """Save a scrobble and sync ratings. Returns {'status', 'title', 'rating_changes'}."""
    event = event_from_payload(payload, client)
    if event is None:
        return {"status": "ignored", "title": None, "rating_changes": 0}

    event_store.init_db(db_path)
    stored = event_store.append_provider_events(db_path, PROVIDER, [event])
    status = "saved" if stored else "duplicate"
    log.info("Plex play %s: %s (%s) by %r at %s",
             status, event.title, event.content_type, event.profile,
             event.timestamp.isoformat(timespec="seconds"))

    rating_changes = 0
    if status == "saved" and client is not None:
        try:
            rating_changes = len(sync_ratings(db_path, client))
        except PlexError as exc:
            log.warning("Plex rating sync failed; it will retry after the next play: %s", exc)
    elif client is None:
        log.info("PLEX_URL or PLEX_TOKEN not set; skipping the Plex rating sync")
    return {"status": status, "title": event.title, "rating_changes": rating_changes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recommend plex", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ratings", help="bring ratings changed in Plex since the last sync into Streamline")
    parser.parse_args(argv)

    client = client_from_config()
    if client is None:
        print("PLEX_URL and PLEX_TOKEN must both be set to sync Plex ratings.", file=sys.stderr)
        return 1
    try:
        changes = sync_ratings(config.EVENT_DB_PATH, client)
    except PlexError as exc:
        print(f"Plex rating sync failed: {exc}", file=sys.stderr)
        return 1

    if not changes:
        print("No rating changes.")
    for change in changes:
        print(f"{change.title} ({change.content_type}): {change.old or 'unrated'} -> {change.new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
