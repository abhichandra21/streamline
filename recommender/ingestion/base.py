import re
from dataclasses import dataclass
from datetime import datetime, timedelta

TV_PATTERN = re.compile(
    r'^(.+?):\s+(?:Season \d+|Book \d+|Limited Series|\d{4}):.+\(Episode \d+\)$'
)

# Bonus content (clips, trailers, featurettes, etc.) that should never reach
# TMDB lookup — these aren't real watch events and produce nonsense matches.
#
# Most keywords must sit at a tag-like position -- the end of the title
# (optionally followed by a short version/numbering suffix like "2", "#3",
# or "Version"), or immediately before a ":"/"|" separator -- not just
# anywhere a bare word happens to match. Deleted scene/song tags also allow
# a directly quoted payload, e.g. 'Deleted Song "Desert Moon"'. Plain
# \b-word matching would flag real titles like "Trailer Park Boys" (starts
# with "trailer") or a "BTS" concert special (the abbreviation collides
# with "behind the scenes"), so the anchoring is deliberately tight: it
# still needs to catch "Cars 3 Trailer 2", "Official Trailer #3", and
# "Moana Sing-Along Version" without also catching "Trailer Park Boys". The
# "bts" alias is dropped entirely since it's too ambiguous with the band
# name to anchor safely.
_TAG_SUFFIX = (
    r"(?:\s*(?:#\s*\d+|\d+|version|cut|edition|extended|unrated|remastered"
    r"|part\s*\d+|vol\.?\s*\d+))?"
)
_BONUS_CONTENT_RE = re.compile(
    r"(?:"
    r"(?:"
    r"\bclip\b"
    r"|\btrailer\b"
    r"|\bfeaturette\b"
    r"|\bbehind the scenes\b"
    r"|\bsing[-\s]?along\b"
    r"|\bvideo journal\b"
    r"|\bsong breakdowns?\b"
    r"|\bpromo(?:tional)?\b"
    r")(?=\s*[:|]|" + _TAG_SUFFIX + r"\s*$)"
    r"|\bdeleted\s+(?:scene|song)\b(?=\s*(?:[:|]|[\"']|$))"
    # "Stunts | More from Pandora's Box | Avatar: The Way of Water" --
    # a featurette breadcrumb segment that doesn't contain any of the
    # keywords above. A plain ">=2 pipes" count would also flag real
    # pipe-styled titles ("Love | Death | Robots"), so this only fires on
    # the specific "more from" breadcrumb phrase, anchored to a segment
    # start (string start or right after a separator).
    r"|(?:^|[:|])\s*more from\b"
    r")",
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
    featurette, etc.), including the "more from ..." featurette-breadcrumb
    phrase.
    """
    non_empty = [p for p in parts if p]
    for part in non_empty:
        if _BONUS_CONTENT_RE.search(part):
            return True
        if any(word in part for word in _NON_LATIN_TRAILER_WORDS):
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
# for TMDB's `with_original_language` search bias. Deliberately excludes
# bare CJK Unified Ideographs (Han characters): they're shared by Chinese
# and Japanese kanji, so without kana or hangul present there is no
# script-only way to tell a Chinese title from a kanji-only Japanese one
# ("怪物" is Japanese, not Chinese, despite being pure Han). Guessing "zh"
# for a Japanese title would apply the wrong language bias, which is worse
# than applying none -- so this only auto-detects scripts that are
# themselves unambiguous.
_SCRIPT_RANGES: list[tuple[str, tuple[tuple[int, int], ...]]] = [
    ("ja", ((0x3040, 0x309F), (0x30A0, 0x30FF))),  # Hiragana, Katakana
    ("ko", ((0xAC00, 0xD7A3), (0x1100, 0x11FF))),  # Hangul
    ("hi", ((0x0900, 0x097F),)),                    # Devanagari
    ("he", ((0x0590, 0x05FF),)),                    # Hebrew
    ("ar", ((0x0600, 0x06FF),)),                    # Arabic
]

# A matched script must account for at least this share of the title's
# letters before it's trusted as a language hint. Without this, a single
# incidental non-Latin character in an otherwise Latin-script title (e.g. a
# place name like "Lost in Translation 東京") would bias the search toward
# a language the title isn't actually in.
_MIN_SCRIPT_SHARE = 0.5


def detect_language_hint(title: str) -> str | None:
    """Detect a non-Latin script in the title and return a TMDB
    `with_original_language` code, or None if the title looks Latin-script
    or the script signal is too ambiguous/incidental to trust.

    TMDB's default search ranking is English-popularity-weighted, so a
    Hindi/Hebrew/Japanese/Korean/Arabic title without this hint tends to
    lose to an unrelated, more popular English title containing the same
    word.
    """
    letters = [ch for ch in title if ch.isalpha()]
    if not letters:
        return None
    for lang, ranges in _SCRIPT_RANGES:
        matched = sum(1 for ch in letters if any(lo <= ord(ch) <= hi for lo, hi in ranges))
        if matched and matched / len(letters) >= _MIN_SCRIPT_SHARE:
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
