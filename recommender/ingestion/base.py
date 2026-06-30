import re
from dataclasses import dataclass
from datetime import datetime, timedelta

TV_PATTERN = re.compile(
    r'^(.+?):\s+(?:Season \d+|Book \d+|Limited Series|\d{4}):.+\(Episode \d+\)$'
)

# Bonus content (clips, trailers, featurettes, etc.) that should never reach
# TMDB lookup — these aren't real watch events and produce nonsense matches.
_BONUS_CONTENT_RE = re.compile(
    r"\bclip\b"
    r"|\btrailer\b"
    r"|\bfeaturette\b"
    r"|\b(?:behind the scenes|bts)\b"
    r"|\bsing[-\s]?along\b"
    r"|\bdeleted\s+(?:scene|song)\b"
    r"|\bvideo journal\b"
    r"|\bsong breakdowns?\b"
    r"|\bpromo(?:tional)?\b",
    re.IGNORECASE,
)

# Words for "trailer" in non-Latin scripts that the ASCII regex above can't
# match. Source exports occasionally render auto-translated trailer labels
# in the title's original language.
_NON_LATIN_TRAILER_WORDS = ("טריילר", "ट्रेलर")


def is_bonus_content(*parts: str) -> bool:
    """Return True if any of the given title parts look like bonus/promo
    content rather than a real watch event.

    Checks each part for known bonus-content keywords (clip, trailer,
    featurette, etc.) and for pipe-separated featurette markers spanning the
    combined parts (e.g. "Stunts | More from X | Movie Title").
    """
    non_empty = [p for p in parts if p]
    for part in non_empty:
        if _BONUS_CONTENT_RE.search(part):
            return True
        if any(word in part for word in _NON_LATIN_TRAILER_WORDS):
            return True
    if sum(p.count("|") for p in non_empty) >= 2:
        return True
    return False


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
