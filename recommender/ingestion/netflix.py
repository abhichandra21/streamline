import csv
import logging
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .base import WatchEvent, classify_title, parse_duration

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
            duration = parse_duration(row["Duration"])
            if duration.total_seconds() < MIN_WATCH_SECONDS:
                continue
            timestamp = datetime.strptime(row["Start Time"], "%Y-%m-%d %H:%M:%S")
            content_type, series_name = classify_title(row["Title"].strip())
            events.append(WatchEvent(
                platform="netflix",
                title=row["Title"].strip(),
                content_type=content_type,
                series_name=series_name,
                watched_duration=duration,
                total_duration=None,
                timestamp=timestamp,
                profile=row["Profile Name"].strip(),
            ))
    return events


def parse(zip_path: str) -> list[WatchEvent]:
    """Parse Netflix watch history from a data export zip.

    Args:
        zip_path: Path to the Netflix data export .zip file.

    Raises:
        FileNotFoundError: If zip_path does not exist.
        ValueError: If the file is not a .zip or the expected CSV is missing.
    """
    p = Path(zip_path)
    if not p.exists():
        raise FileNotFoundError(f"Netflix export not found: {zip_path}")
    if p.suffix != ".zip":
        raise ValueError(f"Netflix path must be a .zip file, got: {zip_path}")

    with tempfile.TemporaryDirectory(prefix="netflix_") as work_dir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(work_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Invalid zip file: {zip_path} ({exc})") from exc

        found = list(Path(work_dir).rglob(_TARGET_CSV))
        if not found:
            raise ValueError(f"{_TARGET_CSV} not found inside {zip_path}")

        log.info("Netflix: reading %s", found[0])
        return _parse_csv(str(found[0]))
