"""Apple TV+ watch history ingestion.

Accepts the raw Apple data export zip (e.g. "Apple Media Services Information
Part 1 of 2.zip"). Handles the nested zip structure automatically:

    outer.zip
      -> Apple_Media_Services.zip
        -> Stores Activity/Other Activity/Video Play Activity.csv
"""

import csv
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .base import WatchEvent, is_bonus_content

log = logging.getLogger("recommender.ingestion.apple_tv")

MIN_WATCH_SECONDS = 5 * 60  # 5 minutes

# Sub-types that are not actual episode/movie watches
_SKIP_SUBTYPES = {"FeaturedPromo", "Preview", "Promotional", "Bonus"}

_TARGET_CSV = "Video Play Activity.csv"


def _find_and_extract_csv(zip_path: str, work_dir: str) -> str | None:
    """Recursively extract nested zips until Video Play Activity.csv is found."""
    to_process = [zip_path]
    seen = set()

    while to_process:
        current = to_process.pop()
        if current in seen:
            continue
        seen.add(current)

        try:
            with zipfile.ZipFile(current, "r") as zf:
                zf.extractall(work_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            log.debug("Skipping %s: %s", current, exc)
            continue

        # Search for the target CSV
        for root, _dirs, files in os.walk(work_dir):
            for fname in files:
                full = os.path.join(root, fname)
                if fname == _TARGET_CSV:
                    return full
                if fname.endswith(".zip") and full not in seen:
                    to_process.append(full)

    return None


def _validate_zip(zip_path: str) -> Path:
    """Validate that zip_path is an existing .zip file."""
    p = Path(zip_path)
    if not p.exists():
        raise FileNotFoundError(f"Apple TV export not found: {zip_path}")
    if p.suffix != ".zip":
        raise ValueError(f"Apple TV path must be a .zip file, got: {zip_path}")
    try:
        with zipfile.ZipFile(p, "r") as zf:
            zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"Invalid zip file: {zip_path} ({exc})") from exc
    return p


def _classify(row: dict) -> tuple[str, str]:
    """Return (content_type, series_name) from a Video Play Activity row.

    TV episodes have Content Episode Name (the show) plus a season or episode.
    Movies have Content Episode Name as the movie title with no season/episode.
    """
    show = row.get("Content Episode Name", "").strip()
    season = row.get("Content Season Name", "").strip()
    ep_num = row.get("Episode Number", "").strip()

    if not show:
        return "movie", row.get("Content Title", "").strip() or "Unknown"

    # Has season or episode number -> TV
    if season or (ep_num and ep_num not in ("", "0", "0.0")):
        return "tv", show

    # No season/episode info -> likely a movie or standalone
    return "movie", show


def _build_title(row: dict, content_type: str, series_name: str) -> str:
    """Build a human-readable title string for the watch event."""
    if content_type == "movie":
        return series_name

    season = row.get("Content Season Name", "").strip()
    ep_title = row.get("Content Title", "").strip()
    ep_num = row.get("Episode Number", "").strip()

    parts = [series_name]
    if season:
        parts.append(season)
    if ep_num and ep_num not in ("", "0", "0.0"):
        # Normalize "3.0" -> "3"
        try:
            parts.append(f"E{int(float(ep_num))}")
        except ValueError:
            parts.append(f"E{ep_num}")
    if ep_title and ep_title != series_name:
        parts.append(ep_title)
    return ": ".join(parts)


def _parse_csv(csv_path: str) -> list[WatchEvent]:
    """Parse Video Play Activity.csv into WatchEvents.

    Apple logs multiple stop events per playback session (scrub, pause, unknown,
    etc.) with identical start times and durations. We dedup by keeping the
    entry with the highest watched duration per (timestamp, series, episode).
    """
    # First pass: collect candidates keyed for dedup
    best: dict[tuple, dict] = {}  # (timestamp_str, series, ep_title) -> row data

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Action Type", "").strip() != "stop":
                continue

            sub_type = row.get("Content Sub-Type", "").strip()
            if sub_type in _SKIP_SUBTYPES:
                continue

            if is_bonus_content(
                row.get("Content Episode Name", ""), row.get("Content Title", "")
            ):
                continue

            try:
                watched_ms = int(row.get("Feature Play Duration", "0") or "0")
            except ValueError:
                watched_ms = 0
            if watched_ms / 1000 < MIN_WATCH_SECONDS:
                continue

            ts_str = row.get("UTC Start Time", "").strip()
            if not ts_str:
                ts_str = row.get("UTC End Time", "").strip()
            if not ts_str:
                continue

            show = row.get("Content Episode Name", "").strip()
            ep_title = row.get("Content Title", "").strip()
            key = (ts_str[:19], show, ep_title)

            prev = best.get(key)
            if prev is None or watched_ms > prev["watched_ms"]:
                best[key] = {"row": row, "watched_ms": watched_ms, "ts_str": ts_str}

    # Second pass: convert deduplicated rows into WatchEvents
    events = []
    for entry in best.values():
        row = entry["row"]
        watched_ms = entry["watched_ms"]
        ts_str = entry["ts_str"]

        try:
            total_ms = int(row.get("Content Length MS", "0") or "0")
        except ValueError:
            total_ms = 0

        try:
            timestamp = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue

        content_type, series_name = _classify(row)
        title = _build_title(row, content_type, series_name)

        events.append(WatchEvent(
            platform="apple_tv",
            title=title,
            content_type=content_type,
            series_name=series_name,
            watched_duration=timedelta(milliseconds=watched_ms),
            total_duration=timedelta(milliseconds=total_ms) if total_ms else None,
            timestamp=timestamp,
            profile="",
        ))

    return events


def parse(zip_path: str) -> list[WatchEvent]:
    """Parse Apple TV watch history from a data export zip.

    Args:
        zip_path: Path to the Apple data export .zip file.

    Raises:
        FileNotFoundError: If zip_path does not exist.
        ValueError: If the file is not a .zip, is malformed, or lacks the expected CSV.
    """
    _validate_zip(zip_path)
    log.debug("Extracting Apple TV data from %s", zip_path)

    with tempfile.TemporaryDirectory(prefix="apple_tv_") as work_dir:
        csv_path = _find_and_extract_csv(zip_path, work_dir)
        if not csv_path:
            raise ValueError(f"{_TARGET_CSV} not found inside {zip_path}")
        log.debug("Found %s", csv_path)
        events = _parse_csv(csv_path)

    log.debug("Apple TV: %d watch events parsed", len(events))
    return events
