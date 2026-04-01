import json
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.base import WatchEvent
from recommender.tmdb_client import TmdbMetadata
from recommender.watch_index import WatchIndex
from recommender import watch_index as wi


def make_event(title, series_name=None, content_type="movie"):
    return WatchEvent(
        platform="prime",
        title=title,
        content_type=content_type,
        series_name=series_name or title,
        watched_duration=timedelta(hours=1),
        total_duration=None,
        timestamp=datetime.now(),
        profile="ADULT",
    )


def make_meta(tmdb_id, title, content_type="movie"):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=[], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )


def test_build_includes_movie_titles():
    events = [make_event("Dilwale Dulhania Le Jayenge (English Subtitled)")]
    index = wi.build(events, {})
    assert ("dilwale dulhania le jayenge", "movie") in index.normalized_titles


def test_build_strips_parentheticals():
    events = [make_event("Oppenheimer (4K UHD)")]
    index = wi.build(events, {})
    assert ("oppenheimer", "movie") in index.normalized_titles
    assert ("oppenheimer (4k uhd)", "movie") not in index.normalized_titles


def test_build_includes_tv_series_names():
    events = [make_event("Episode 1-Downton Abbey - Season 3", series_name="Downton Abbey", content_type="tv")]
    index = wi.build(events, {})
    assert ("downton abbey", "tv") in index.normalized_titles


def test_build_deduplicates():
    events = [
        make_event("Chef"),
        make_event("Chef"),
        make_event("Chef (Hindi)"),
    ]
    index = wi.build(events, {})
    assert len([t for t in index.normalized_titles if "chef" in t[0]]) == 1


def test_build_stores_tmdb_id():
    events = [make_event("Fleabag", series_name="Fleabag", content_type="tv")]
    meta = make_meta(tmdb_id=67452, title="Fleabag", content_type="tv")
    index = wi.build(events, {"Fleabag": meta})
    assert 67452 in index.tmdb_ids


def test_is_watched_by_tmdb_id():
    meta = make_meta(tmdb_id=12345, title="Fleabag", content_type="tv")
    index = WatchIndex(tmdb_ids={12345}, normalized_titles=set(), entries=[])
    assert index.is_watched(meta) is True


def test_is_watched_by_title_fallback():
    meta = make_meta(tmdb_id=0, title="Downton Abbey", content_type="tv")
    index = WatchIndex(tmdb_ids=set(), normalized_titles={("downton abbey", "tv")}, entries=[])
    assert index.is_watched(meta) is True


def test_is_watched_false_for_unknown():
    meta = make_meta(tmdb_id=99999, title="Broadchurch", content_type="tv")
    index = WatchIndex(tmdb_ids={12345}, normalized_titles={("fleabag", "tv")}, entries=[])
    assert index.is_watched(meta) is False


def test_is_watched_content_type_aware():
    """A watched TV show should not block a movie with the same title."""
    meta_movie = make_meta(tmdb_id=0, title="Fargo", content_type="movie")
    index = WatchIndex(tmdb_ids=set(), normalized_titles={("fargo", "tv")}, entries=[])
    assert index.is_watched(meta_movie) is False


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "index.json")
    events = [make_event("Fleabag", series_name="Fleabag", content_type="tv")]
    meta = make_meta(tmdb_id=67452, title="Fleabag", content_type="tv")
    index = wi.build(events, {"Fleabag": meta})
    wi.save(index, path)
    loaded = wi.load(path)
    assert 67452 in loaded.tmdb_ids
    assert ("fleabag", "tv") in loaded.normalized_titles
