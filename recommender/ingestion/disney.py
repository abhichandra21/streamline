import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import config
from .base import WatchEvent

log = logging.getLogger("recommender.ingestion.disney")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRAILER_RE = re.compile(r"\btrailer\b|\|\s*trailer", re.IGNORECASE)


def _allowed_profiles() -> set[str]:
    profiles = getattr(config, "DISNEY_PROFILES", None) or []
    return {p.strip() for p in profiles if p and p.strip()}


def _normalize(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


def _extract_rows(pdf_path: str) -> list[dict]:
    """Extract Titles Watched rows from a Disney+ data export PDF.

    Disney's export renders the watched-titles table identically across all
    its pages. We scan every page's tables and accept any row whose first
    cell is an ISO date — that uniquely identifies a watch event and skips
    headers, the franchise list, the brand list, and account/profile tables.
    """
    import pdfplumber

    rows: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row or len(row) < 5:
                        continue
                    cells = [(c or "").strip() for c in row]
                    if not _DATE_RE.match(cells[0]):
                        continue
                    rows.append({
                        "date": cells[0],
                        "service": cells[1],
                        "profile": cells[2],
                        "program": _normalize(cells[3]),
                        "season": _normalize(cells[4]),
                    })
    return rows


def parse(path: str) -> list[WatchEvent]:
    """Parse Disney+ watch history from the data-subject-request PDF export.

    Disney delivers history as a PDF (no CSV option). The relevant section is
    'TITLES WATCHED' with columns: Date, Service, Profile, Program, Season.
    Note: the 'Season' column actually carries the series name; 'Program' is
    the episode title for TV and the movie title for film (Season blank).

    Transforms:
      * Restrict to profiles in config.DISNEY_PROFILES (empty list = keep all).
      * Drop trailers (Program or Season contains 'Trailer').
      * Treat non-blank Season as the TV series; blank Season => movie.
      * Collapse same-day duplicates per (profile, content_type, series).
        Disney logs each restart/episode as a separate row, which would
        otherwise massively over-weight kid-co-watched series.

    Durations are synthesized from config.MANUAL_TV_DURATION_MINUTES /
    MANUAL_MOVIE_DURATION_MINUTES, mirroring the manual ingestion path,
    because the export contains no duration or completion data.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Disney+ export not found: {path}")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"Disney+ path must be a .pdf file, got: {path}")

    rows = _extract_rows(path)
    log.info("Disney+: read %d raw rows from %s", len(rows), path)

    allowed = _allowed_profiles()
    tv_duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES)
    movie_duration = timedelta(minutes=config.MANUAL_MOVIE_DURATION_MINUTES)

    seen: set[tuple] = set()
    events: list[WatchEvent] = []

    for row in rows:
        profile = row["profile"]
        if allowed and profile not in allowed:
            continue

        program = row["program"]
        season = row["season"]
        if not program and not season:
            continue
        if _TRAILER_RE.search(program) or _TRAILER_RE.search(season):
            continue

        if season:
            content_type = "tv"
            series_name = season
            duration = tv_duration
        else:
            content_type = "movie"
            series_name = program
            duration = movie_duration

        try:
            timestamp = datetime.strptime(row["date"], "%Y-%m-%d")
        except ValueError:
            continue

        key = (row["date"], profile, content_type, series_name.lower())
        if key in seen:
            continue
        seen.add(key)

        events.append(WatchEvent(
            platform="disney",
            title=series_name,
            content_type=content_type,
            series_name=series_name,
            watched_duration=duration,
            total_duration=duration,
            timestamp=timestamp,
            profile=profile,
        ))

    return events
