import re
from dataclasses import dataclass
from datetime import datetime, timedelta

TV_PATTERN = re.compile(
    r'^(.+?):\s+(?:Season \d+|Book \d+|Limited Series|\d{4}):.+\(Episode \d+\)$'
)


@dataclass
class WatchEvent:
    platform: str           # "netflix", "prime", "disney", "hbo"
    title: str              # raw full title string
    content_type: str       # "tv" or "movie"
    series_name: str        # TV: show name; Movie: same as title
    watched_duration: timedelta
    total_duration: timedelta | None   # filled by TMDB lookup
    timestamp: datetime
    profile: str
    release_year_hint: int | None = None  # source year for TMDB matching


def classify_title(title: str) -> tuple[str, str]:
    """
    Returns (content_type, series_name).
    TV: "Show Name: Season/Book/etc X: Episode (N)" -> ("tv", "Show Name")
    Movie: anything else -> ("movie", title)
    """
    match = TV_PATTERN.match(title)
    if match:
        return "tv", match.group(1).strip()
    return "movie", title


def parse_duration(s: str) -> timedelta:
    """Parse 'HH:MM:SS' into timedelta."""
    h, m, sec = s.strip().split(":")
    return timedelta(hours=int(h), minutes=int(m), seconds=int(sec))
