import csv
from datetime import datetime

from .base import WatchEvent, classify_title, parse_duration

MIN_WATCH_SECONDS = 5 * 60   # 5 minutes


def parse(filepath: str) -> list[WatchEvent]:
    """Parse Netflix ViewingActivity.csv into WatchEvents."""
    events = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip trailers and promotional content
            if row["Supplemental Video Type"].strip():
                continue
            duration = parse_duration(row["Duration"])
            # Skip very short watches (accidental plays)
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
