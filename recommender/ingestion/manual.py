import re
from datetime import datetime, timedelta

from .base import WatchEvent

_YEAR_RE = re.compile(r'\s+(19|20)\d{2}$')
_NEUTRAL_DATE = datetime(2022, 1, 1)
_TV_DURATION = timedelta(minutes=45)
_MOVIE_DURATION = timedelta(hours=2)


def _strip_year(name: str) -> str:
    return _YEAR_RE.sub('', name).strip()


def parse(tv_path: str, movies_path: str) -> list[WatchEvent]:
    events = []
    seen: set[str] = set()

    with open(tv_path, encoding='utf-8') as f:
        for line in f:
            title = line.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            events.append(WatchEvent(
                platform='manual',
                title=title,
                content_type='tv',
                series_name=title,
                watched_duration=_TV_DURATION,
                total_duration=_TV_DURATION,
                timestamp=_NEUTRAL_DATE,
                profile='',
            ))

    with open(movies_path, encoding='utf-8') as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            title = _strip_year(raw)
            if not title or title in seen:
                continue
            seen.add(title)
            events.append(WatchEvent(
                platform='manual',
                title=title,
                content_type='movie',
                series_name=title,
                watched_duration=_MOVIE_DURATION,
                total_duration=_MOVIE_DURATION,
                timestamp=_NEUTRAL_DATE,
                profile='',
            ))

    return events
