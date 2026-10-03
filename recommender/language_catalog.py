"""Saved, IMDb-rated lists of titles in one original language, for the Find page.

TMDB's own votes are too thin for non-English titles to sort or filter on, and
the titles worth seeing are spread across TMDB's whole popularity order rather
than bunched at the top. So a background build reads every TMDB Discover page
for the language over Find's longest period, attaches IMDb ratings, and saves
the list. Find then filters and sorts that list locally, with no TMDB calls
per page view.

A build writes a new file and renames it into place only when complete, so a
failed build keeps the previous list.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests
from dateutil.relativedelta import relativedelta

from recommender import imdb_ratings
from recommender.imdb_ratings import ImdbRating
from recommender.tmdb_client import CatalogTitle, TmdbClient, TmdbRateLimitError

log = logging.getLogger(__name__)

# (code, label). Codes are TMDB original_language values and form keys.
LANGUAGE_OPTIONS = (("hi", "Hindi"),)
LANGUAGES = dict(LANGUAGE_OPTIONS)
# Matches Find's longest period, so every period is a filter on one list.
BUILD_YEARS = 10
REBUILD_AGE = timedelta(days=1)
# TMDB refuses Discover pages past 500.
MAX_DISCOVER_PAGES = 500
_LOOKUP_CONCURRENCY = 8
_RETRIES = 5
BUILD_JOB_LABEL = "building language lists"


@dataclass(frozen=True)
class LanguageTitle:
    title: CatalogTitle
    release_date: date | None
    popularity: float
    genre_ids: frozenset[int]
    imdb: ImdbRating


@dataclass(frozen=True)
class LanguageList:
    language: str
    built_at: datetime
    titles: tuple[LanguageTitle, ...]


def list_path(cache_dir: str | Path, language: str) -> Path:
    return Path(cache_dir) / f"language_{language}.json"


def _retry_transient(call: Callable):
    """Run call, waiting out TMDB rate limits and retrying timeouts and dropped
    connections. A build makes thousands of calls, so one blip must not fail it.
    HTTP errors and anything still failing after the retries propagate."""
    for attempt in range(_RETRIES):
        try:
            return call()
        except TmdbRateLimitError as exc:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(exc.retry_after_seconds or 1.0)
        except requests.ConnectionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def _imdb_id(tmdb: TmdbClient, tmdb_id: int, content_type: str) -> str | None:
    try:
        return _retry_transient(lambda: tmdb.get_imdb_id(tmdb_id, content_type))
    except requests.HTTPError as exc:
        # A title removed from TMDB since Discover listed it has no IMDb ID to find.
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def build(
    tmdb: TmdbClient,
    language: str,
    imdb_db_path: str | Path,
    cache_dir: str | Path,
    today: date | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Build and save the list for one language. Returns the number of titles kept.

    Only titles with an IMDb rating are kept; the vote floor is applied when
    Find reads the list, so changing it does not need a rebuild. Raises on any
    TMDB failure other than a rate limit it can wait out, leaving the previous
    list in place: a list silently missing titles would answer a different
    question.
    """
    if imdb_ratings.refreshed_at(imdb_db_path) is None:
        raise RuntimeError("IMDb ratings are not downloaded yet; run ./recommend setup --refresh-imdb")
    end = today or date.today()
    start = end - relativedelta(years=BUILD_YEARS)

    raw_rows: list[tuple[str, dict]] = []
    seen: set[tuple[str, int]] = set()
    for content_type in ("movie", "tv"):
        page = 1
        while True:
            results, total_pages = _retry_transient(
                lambda: tmdb.discover_language_page(content_type, language, start, end, page))
            for item in results:
                key = (content_type, int(item["id"]))
                # Popularity can shift between page reads; keep the first sighting.
                if key not in seen:
                    seen.add(key)
                    raw_rows.append((content_type, item))
            if not results or page >= min(total_pages, MAX_DISCOVER_PAGES):
                break
            page += 1

    done = 0
    done_lock = threading.Lock()
    total = len(raw_rows)

    def _lookup(row: tuple[str, dict]) -> str | None:
        nonlocal done
        imdb_id = _imdb_id(tmdb, int(row[1]["id"]), row[0])
        with done_lock:
            done += 1
            completed = done
        if progress and completed % 50 == 0:
            progress(completed, total)
        return imdb_id

    with ThreadPoolExecutor(max_workers=_LOOKUP_CONCURRENCY) as pool:
        imdb_ids = list(pool.map(_lookup, raw_rows))
    ratings = imdb_ratings.lookup(imdb_db_path, [i for i in imdb_ids if i])

    titles = []
    for (content_type, item), imdb_id in zip(raw_rows, imdb_ids):
        rating = ratings.get(imdb_id) if imdb_id else None
        if rating is None:
            continue
        row = TmdbClient._parse_catalog_row(item, content_type)
        titles.append({
            "tmdb_id": row.tmdb_id,
            "content_type": row.content_type,
            "title": row.title,
            "year": row.year,
            "poster_path": row.poster_path,
            "overview": row.overview,
            "vote_average": row.vote_average,
            "vote_count": row.vote_count,
            "release_date": item.get("first_air_date" if content_type == "tv" else "release_date") or None,
            "popularity": float(item.get("popularity") or 0.0),
            "genre_ids": [int(g) for g in item.get("genre_ids") or []],
            "imdb_id": imdb_id,
            "imdb_rating": rating.rating,
            "imdb_votes": rating.votes,
        })

    path = list_path(cache_dir, language)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.",
                                     suffix=".tmp", delete=False) as f:
        json.dump({
            "language": language,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "titles": titles,
        }, f)
        tmp = Path(f.name)
    try:
        tmp.chmod(0o644)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Built %s list: %d rated titles from %d TMDB titles", language, len(titles), total)
    if progress:
        progress(total, total)
    return len(titles)


def load(cache_dir: str | Path, language: str) -> LanguageList | None:
    """The saved list, or None when it has not been built or cannot be read."""
    path = list_path(cache_dir, language)
    try:
        data = json.loads(path.read_text())
        titles = tuple(
            LanguageTitle(
                title=CatalogTitle(
                    tmdb_id=int(t["tmdb_id"]), content_type=t["content_type"], title=t["title"],
                    year=t.get("year"), poster_path=t.get("poster_path"), overview=t.get("overview") or "",
                    vote_average=float(t.get("vote_average") or 0.0), vote_count=int(t.get("vote_count") or 0),
                ),
                release_date=_parse_date(t.get("release_date")),
                popularity=float(t.get("popularity") or 0.0),
                genre_ids=frozenset(int(g) for g in t.get("genre_ids") or []),
                imdb=ImdbRating(rating=float(t["imdb_rating"]), votes=int(t["imdb_votes"])),
            )
            for t in data["titles"]
        )
        return LanguageList(language=data["language"], built_at=datetime.fromisoformat(data["built_at"]),
                            titles=titles)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Language list unreadable at %s: %s", path, exc)
        return None


def build_is_due(cache_dir: str | Path, language: str, now: datetime | None = None) -> bool:
    saved = load(cache_dir, language)
    now = now or datetime.now(timezone.utc)
    return saved is None or now - saved.built_at >= REBUILD_AGE
