"""Parser for the WBD/Max data-export bundle.

The bundle is a directory of CSVs (not a zip). The only file containing watch
history is `*_engagement_data.csv`, which is unusual in shape: each row is a
single feature value, and values that happen to be lists are written with the
Python-repr `[item1, item2, ...]` syntax. That repr collides with the comma
delimiter, so the list spans multiple CSV columns. We reassemble it by joining
the row and stripping the brackets.

Titles in the list use the form `series name-s<N>-e<M>` for TV episodes and a
bare title for movies. The export gives no per-play timestamp, completion
percentage, or device. We synthesize a single ingestion timestamp (same scheme
as `manual.py`) and emit one WatchEvent per unique title.
"""

import csv
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import config

from .base import WatchEvent

log = logging.getLogger("recommender.ingestion.hbo")

_EPISODE_RE = re.compile(r"^(?P<show>.+?)-s(?P<season>\d+)-e(?P<episode>\d+)$")
_TRAILER_HINTS = ("trailer", "promo_", "teaser")


def _ingest_timestamp() -> datetime:
    ts = config.MANUAL_TIMESTAMP
    if ts == "now":
        return datetime.now()
    try:
        return datetime.strptime(ts, "%Y-%m-%d")
    except ValueError:
        return datetime.now()


def _find_engagement_csv(directory: Path) -> Path | None:
    """Pick the engagement_data CSV that contains the title list.

    The bundle may include multiple `*_engagement_data.csv` files; only one
    holds the title arrays. We choose by file size (the title-bearing file is
    materially larger than the segment-only or empty variants).
    """
    candidates = sorted(directory.glob("*_engagement_data.csv"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _extract_title_rows(filepath: Path) -> list[list[str]]:
    """Return rows whose items look like a real title list.

    A row qualifies if any cell matches the `-sNN-eNN` episode pattern.
    """
    rows: list[list[str]] = []
    with open(filepath, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            joined = ",".join(row)
            if "-s" in joined and re.search(r"-s\d+-e\d+", joined):
                rows.append(row)
    return rows


def _normalize_items(row: list[str]) -> list[str]:
    """Strip brackets/whitespace from a comma-split list row."""
    items = []
    for raw in row:
        item = raw.strip().lstrip("[").rstrip("]").strip()
        if item:
            items.append(item)
    return items


def _classify(item: str) -> tuple[str, str, str] | None:
    """Return (content_type, series_name, display_title) or None to skip.

    Drops trailers and other promotional content. Strips the optional
    `movie_` / `series_` / `promo_` type-prefix used in the typed
    "recently-watched" row.
    """
    lower = item.lower()
    if any(h in lower for h in _TRAILER_HINTS):
        return None

    if lower.startswith("movie_"):
        item = item[6:].strip()
    elif lower.startswith("series_"):
        item = item[7:].strip()
    elif lower.startswith("promo_"):
        return None

    if not item:
        return None

    match = _EPISODE_RE.match(item)
    if match:
        show = match.group("show").strip()
        return "tv", show, item
    return "movie", item, item


def parse(path: str) -> list[WatchEvent]:
    """Parse the Max/WBD bundle directory and return WatchEvents.

    Args:
        path: Directory containing the WBD CSV export.

    Raises:
        FileNotFoundError: directory does not exist.
        ValueError: bundle is not a directory or has no engagement file.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Max export directory not found: {path}")
    if not p.is_dir():
        raise ValueError(f"Max path must be a directory, got: {path}")

    engagement = _find_engagement_csv(p)
    if engagement is None:
        raise ValueError(f"No *_engagement_data.csv found in {path}")

    log.debug("Max: reading %s", engagement)
    title_rows = _extract_title_rows(engagement)
    if not title_rows:
        log.warning("Max engagement file %s has no title rows", engagement)
        return []

    timestamp = _ingest_timestamp()
    tv_duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES)
    movie_duration = timedelta(minutes=config.MANUAL_MOVIE_DURATION_MINUTES)

    seen: set[tuple[str, str]] = set()
    events: list[WatchEvent] = []

    for row in title_rows:
        for item in _normalize_items(row):
            classified = _classify(item)
            if classified is None:
                continue
            content_type, series_name, display_title = classified
            key = (content_type, display_title.lower())
            if key in seen:
                continue
            seen.add(key)
            duration = tv_duration if content_type == "tv" else movie_duration
            events.append(WatchEvent(
                platform="hbo",
                title=display_title,
                content_type=content_type,
                series_name=series_name,
                watched_duration=duration,
                total_duration=duration,
                timestamp=timestamp,
                profile="",
            ))

    log.debug("Max: parsed %d events from %s", len(events), engagement.name)
    return events
