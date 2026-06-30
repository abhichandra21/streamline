import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import config
from .base import WatchEvent, detect_language_hint, is_bonus_content

log = logging.getLogger("recommender.ingestion.disney")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
      * Drop bonus content: trailers, clips, featurettes, sing-alongs, etc.
        (see recommender.ingestion.base.is_bonus_content).
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

    try:
        rows = _extract_rows(path)
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        # pdfplumber/pdfminer raise their own exception hierarchies on
        # corrupt/encrypted/unsupported PDFs. Normalize to ValueError so
        # the setup loader treats it as a provider-validation failure
        # instead of crashing.
        raise ValueError(f"Failed to parse Disney+ PDF: {path} ({exc})") from exc
    log.debug("Disney+: read %d raw rows from %s", len(rows), path)

    allowed = _allowed_profiles()
    tv_duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES)
    movie_duration = timedelta(minutes=config.MANUAL_MOVIE_DURATION_MINUTES)

    seen: set[tuple] = set()
    events: list[WatchEvent] = []

    for row in rows:
        if row["service"].strip().lower() != "disney":
            continue

        profile = row["profile"]
        if allowed and profile not in allowed:
            continue

        program = row["program"]
        season = row["season"]
        if not program and not season:
            continue
        if is_bonus_content(program, season):
            continue

        if season:
            content_type = "tv"
            series_name = season
            # Episode-distinct title so the rewatch signal in
            # signals.compute_scores() doesn't treat every Disney TV
            # event for a series as a rewatch of one episode.
            title = program or season
            duration = tv_duration
        else:
            content_type = "movie"
            series_name = program
            title = program
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
            title=title,
            content_type=content_type,
            series_name=series_name,
            watched_duration=duration,
            total_duration=duration,
            timestamp=timestamp,
            profile=profile,
            language_hint=detect_language_hint(series_name),
        ))

    return events
