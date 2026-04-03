import re
from datetime import datetime, timedelta

import config
from .base import WatchEvent

_YEAR_RE = re.compile(r'\s+(19|20)\d{2}$')


def _get_timestamp() -> datetime:
    ts = config.MANUAL_TIMESTAMP
    if ts == "now":
        return datetime.now()
    try:
        return datetime.strptime(ts, "%Y-%m-%d")
    except ValueError:
        return datetime.now()


def _strip_year(name: str) -> str:
    return _YEAR_RE.sub('', name).strip()


def parse(tv_path: str, movies_path: str) -> list[WatchEvent]:
    events = []
    seen_tv: set[str] = set()
    seen_movies: set[str] = set()
    timestamp = _get_timestamp()
    tv_duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES)
    movie_duration = timedelta(minutes=config.MANUAL_MOVIE_DURATION_MINUTES)

    with open(tv_path, encoding='utf-8') as f:
        for line in f:
            title = line.strip()
            if not title or title in seen_tv:
                continue
            seen_tv.add(title)
            events.append(WatchEvent(
                platform='manual',
                title=title,
                content_type='tv',
                series_name=title,
                watched_duration=tv_duration,
                total_duration=tv_duration,
                timestamp=timestamp,
                profile='',
            ))

    with open(movies_path, encoding='utf-8') as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            title = _strip_year(raw)
            if not title or title in seen_movies:
                continue
            seen_movies.add(title)
            events.append(WatchEvent(
                platform='manual',
                title=title,
                content_type='movie',
                series_name=title,
                watched_duration=movie_duration,
                total_duration=movie_duration,
                timestamp=timestamp,
                profile='',
            ))

    return events
