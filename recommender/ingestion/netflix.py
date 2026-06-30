import csv
import logging
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .base import WatchEvent, classify_title, detect_language_hint, is_bonus_content, parse_duration

log = logging.getLogger("recommender.ingestion.netflix")

MIN_WATCH_SECONDS = 5 * 60   # 5 minutes

_TARGET_CSV = "ViewingActivity.csv"


def _parse_csv(filepath: str) -> list[WatchEvent]:
    """Parse Netflix ViewingActivity.csv into WatchEvents."""
    events = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Supplemental Video Type"].strip():
                continue
            if is_bonus_content(row["Title"]):
                continue
            duration = parse_duration(row["Duration"])
            if duration.total_seconds() < MIN_WATCH_SECONDS:
                continue
            timestamp = datetime.strptime(row["Start Time"], "%Y-%m-%d %H:%M:%S")
            title = row["Title"].strip()
            content_type, series_name = classify_title(title)
            events.append(WatchEvent(
                platform="netflix",
                title=title,
                content_type=content_type,
                series_name=series_name,
                watched_duration=duration,
                total_duration=None,
                timestamp=timestamp,
                language_hint=detect_language_hint(title),
                profile=row["Profile Name"].strip(),
            ))
    return events


def parse(path: str) -> list[WatchEvent]:
    """Parse Netflix watch history from a data export zip.

    Args:
        path: Path to the Netflix data export .zip file.

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If the file type is unsupported or the expected CSV is missing.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Netflix export not found: {path}")
    if p.suffix != ".zip":
        raise ValueError(f"Netflix path must be a .zip file, got: {path}")

    with tempfile.TemporaryDirectory(prefix="netflix_") as work_dir:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(work_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid zip file: {path} ({exc})") from exc

        found = list(Path(work_dir).rglob(_TARGET_CSV))
        if not found:
            raise ValueError(f"{_TARGET_CSV} not found inside {path}")

        log.debug("Netflix: reading %s", found[0])
        return _parse_csv(str(found[0]))
