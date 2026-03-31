import os
from datetime import timedelta, datetime
from recommender.ingestion.netflix import parse

FIXTURE = os.path.join(os.path.dirname(__file__), "../fixtures/netflix_sample.csv")


def test_skips_trailers():
    events = parse(FIXTURE)
    titles = [e.title for e in events]
    assert not any("TRAILER" in t or "Trailer:" in t for t in titles)


def test_skips_short_watches():
    events = parse(FIXTURE)
    titles = [e.title for e in events]
    assert not any("Search Party" in t for t in titles)


def test_tv_event_classification():
    events = parse(FIXTURE)
    tv_events = [e for e in events if e.content_type == "tv"]
    assert len(tv_events) == 2
    assert all(e.series_name == "Avatar: The Last Airbender" for e in tv_events)


def test_movie_event_classification():
    events = parse(FIXTURE)
    movie_events = [e for e in events if e.content_type == "movie"]
    assert len(movie_events) == 1
    assert movie_events[0].title == "14 Peaks: Nothing Is Impossible"


def test_duration_parsed():
    events = parse(FIXTURE)
    avatar = next(e for e in events if "Book 3: Day of" in e.title)
    assert avatar.watched_duration == timedelta(hours=0, minutes=45, seconds=3)


def test_timestamp_parsed():
    events = parse(FIXTURE)
    avatar = next(e for e in events if "Book 3: Day of" in e.title)
    assert avatar.timestamp == datetime(2026, 3, 16, 20, 22, 18)


def test_platform_set():
    events = parse(FIXTURE)
    assert all(e.platform == "netflix" for e in events)


def test_total_duration_none():
    events = parse(FIXTURE)
    assert all(e.total_duration is None for e in events)
