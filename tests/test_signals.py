import math
from datetime import datetime, timedelta
from recommender.ingestion.base import WatchEvent
from recommender.tmdb_client import TmdbMetadata
from recommender.signals import compute_scores


def make_event(title, series, ct, minutes, days_ago, profile="Ratna"):
    ts = datetime(2026, 3, 31) - timedelta(days=days_ago)
    return WatchEvent(
        platform="netflix", title=title, content_type=ct,
        series_name=series, watched_duration=timedelta(minutes=minutes),
        total_duration=None, timestamp=ts, profile=profile,
    )


def make_tv_meta(title, runtime=23):
    return TmdbMetadata(
        tmdb_id=1, content_type="tv", title=title,
        runtime_minutes=runtime, vote_average=8.0, vote_count=1000,
    )


def make_movie_meta(title, runtime=90):
    return TmdbMetadata(
        tmdb_id=2, content_type="movie", title=title,
        runtime_minutes=runtime, vote_average=7.0, vote_count=500,
    )


def test_tv_events_grouped_by_series():
    events = [
        make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 22, 5),
        make_event("Show: Season 1: Ep2 (Episode 2)", "Show", "tv", 22, 4),
    ]
    scores = compute_scores(events, {})
    assert "Show" in scores
    assert len(scores) == 1   # grouped, not two separate entries


def test_movie_keyed_by_title():
    events = [make_event("My Movie", "My Movie", "movie", 90, 10)]
    scores = compute_scores(events, {})
    assert "My Movie" in scores


def test_full_completion_scores_high():
    events = [make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 23, 1)]
    meta = {"Show": make_tv_meta("Show", runtime=23)}
    scores = compute_scores(events, meta, recency_half_life_days=90)
    # completion=1.0, rewatch=0, recency≈exp(-1/90)≈0.989
    assert scores["Show"] > 0.6


def test_typed_metadata_keys_use_runtime():
    events = [make_event("Short Movie", "Short Movie", "movie", 45, 0)]
    string_keyed = {"Short Movie": make_movie_meta("Short Movie", runtime=45)}
    typed_keyed = {("Short Movie", "movie"): make_movie_meta("Short Movie", runtime=45)}

    assert compute_scores(events, typed_keyed)["Short Movie"] == compute_scores(events, string_keyed)["Short Movie"]


def test_partial_watch_scores_lower():
    full_events = [make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 23, 1)]
    partial_events = [make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 5, 1)]
    meta = {"Show": make_tv_meta("Show", runtime=23)}
    full_score = compute_scores(full_events, meta)["Show"]
    partial_score = compute_scores(partial_events, meta)["Show"]
    assert partial_score < full_score


def test_rewatch_bonus_increases_score():
    # Two events for the same episode = one rewatch
    events = [
        make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 22, 5),
        make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 22, 3),
    ]
    single_events = [
        make_event("Show: Season 1: Ep1 (Episode 1)", "Show", "tv", 22, 5),
    ]
    meta = {"Show": make_tv_meta("Show")}
    rewatch_score = compute_scores(events, meta)["Show"]
    single_score = compute_scores(single_events, meta)["Show"]
    assert rewatch_score > single_score


def test_recent_watch_scores_higher_than_old():
    recent = [make_event("Movie", "Movie", "movie", 90, 1)]
    old = [make_event("Movie", "Movie", "movie", 90, 200)]
    meta = {"Movie": make_movie_meta("Movie")}
    recent_score = compute_scores(recent, meta)["Movie"]
    old_score = compute_scores(old, meta)["Movie"]
    assert recent_score > old_score


def test_score_range():
    events = [make_event("Movie", "Movie", "movie", 90, 30)]
    meta = {"Movie": make_movie_meta("Movie")}
    score = compute_scores(events, meta)["Movie"]
    assert 0.0 <= score <= 1.0


def test_default_runtime_used_when_no_metadata():
    # Should not raise — uses fallback runtime
    events = [make_event("Unknown: Season 1: Ep1 (Episode 1)", "Unknown", "tv", 20, 5)]
    scores = compute_scores(events, {})
    assert "Unknown" in scores
