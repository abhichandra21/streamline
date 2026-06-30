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
    language_hint: str | None = None      # TMDB original_language code (e.g. "hi"), if detected


# Non-Latin scripts whose presence in a title is a strong language signal
# for TMDB's `with_original_language` search bias. Checked in this order so
# Japanese (which mixes kanji with kana) is detected before the broader CJK
# ideograph range is attributed to Chinese.
_SCRIPT_RANGES: list[tuple[str, tuple[tuple[int, int], ...]]] = [
    ("ja", ((0x3040, 0x309F), (0x30A0, 0x30FF))),  # Hiragana, Katakana
    ("ko", ((0xAC00, 0xD7A3), (0x1100, 0x11FF))),  # Hangul
    ("zh", ((0x4E00, 0x9FFF),)),                    # CJK Unified Ideographs
    ("hi", ((0x0900, 0x097F),)),                    # Devanagari
    ("he", ((0x0590, 0x05FF),)),                    # Hebrew
    ("ar", ((0x0600, 0x06FF),)),                    # Arabic
]


def detect_language_hint(title: str) -> str | None:
    """Detect a non-Latin script in the title and return a TMDB
    `with_original_language` code, or None if the title looks Latin-script.

    TMDB's default search ranking is English-popularity-weighted, so a
    Hindi/Hebrew/CJK/Arabic title without this hint tends to lose to an
    unrelated, more popular English title containing the same word.
    """
    for lang, ranges in _SCRIPT_RANGES:
        for ch in title:
            code = ord(ch)
            if any(lo <= code <= hi for lo, hi in ranges):
                return lang
    return None


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
