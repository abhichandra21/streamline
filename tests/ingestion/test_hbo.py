from datetime import datetime, timedelta
from pathlib import Path

import pytest

from recommender.ingestion.hbo import parse


def _write_engagement(directory: Path, lines: list[str]) -> Path:
    """Write a fake engagement_data CSV. Each entry in `lines` becomes one row."""
    path = directory / "ABC123_engagement_data.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def bundle(tmp_path):
    return tmp_path


def test_parses_movies_and_tv_episodes(bundle):
    _write_engagement(bundle, [
        "roku",
        "[roku, web]",
        "4",
        "[some movie, show name-s1-e1, show name-s1-e2]",
    ])
    events = parse(str(bundle))
    by_title = {e.title: e for e in events}
    assert set(by_title.keys()) == {"some movie", "show name-s1-e1", "show name-s1-e2"}

    movie = by_title["some movie"]
    assert movie.content_type == "movie"
    assert movie.series_name == "some movie"
    assert movie.platform == "hbo"

    ep = by_title["show name-s1-e1"]
    assert ep.content_type == "tv"
    assert ep.series_name == "show name"


def test_filters_trailers_and_promos(bundle):
    _write_engagement(bundle, [
        "[real movie, real show-s1-e1, eddington trailer, promo_foo bar]",
    ])
    events = parse(str(bundle))
    titles = {e.title for e in events}
    assert titles == {"real movie", "real show-s1-e1"}


def test_strips_typed_prefixes(bundle):
    _write_engagement(bundle, [
        "[movie_one battle after another, series_succession-s4-e10, real show-s1-e1]",
    ])
    events = parse(str(bundle))
    by_title = {e.title: e.content_type for e in events}
    assert by_title["one battle after another"] == "movie"
    assert by_title["succession-s4-e10"] == "tv"


def test_dedupes_across_multiple_title_rows(bundle):
    _write_engagement(bundle, [
        "[movie a, show-s1-e1]",
        "[show-s1-e1, movie a, movie b]",
    ])
    events = parse(str(bundle))
    titles = sorted(e.title for e in events)
    assert titles == ["movie a", "movie b", "show-s1-e1"]


def test_handles_titles_with_special_chars(bundle):
    _write_engagement(bundle, [
        "[who's talking to chris wallace?-s1-e22, blue valentine]",
    ])
    events = parse(str(bundle))
    by_title = {e.title: e for e in events}
    assert "who's talking to chris wallace?-s1-e22" in by_title
    assert by_title["who's talking to chris wallace?-s1-e22"].series_name == "who's talking to chris wallace?"


def test_picks_largest_engagement_file(bundle):
    # Small segment-only file
    (bundle / "AAA_engagement_data.csv").write_text("Prestige Superfans\n", encoding="utf-8")
    # Larger title-bearing file
    _write_engagement(bundle, [
        "[real movie, real show-s1-e1]",
    ])
    events = parse(str(bundle))
    titles = {e.title for e in events}
    assert titles == {"real movie", "real show-s1-e1"}


def test_returns_empty_when_no_title_rows(bundle):
    _write_engagement(bundle, ["Prestige Superfans", "false", "1.0"])
    events = parse(str(bundle))
    assert events == []


def test_raises_when_directory_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(str(tmp_path / "does-not-exist"))


def test_raises_when_path_is_a_file(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a directory"):
        parse(str(f))


def test_raises_when_no_engagement_file(tmp_path):
    (tmp_path / "other.csv").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="No \\*_engagement_data.csv"):
        parse(str(tmp_path))


def test_event_fields_use_manual_defaults(bundle, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "MANUAL_TV_DURATION_MINUTES", 30)
    monkeypatch.setattr(cfg, "MANUAL_MOVIE_DURATION_MINUTES", 100)
    monkeypatch.setattr(cfg, "MANUAL_TIMESTAMP", "2025-01-15")
    _write_engagement(bundle, ["[some movie, show-s1-e1]"])
    events = parse(str(bundle))
    movie = next(e for e in events if e.content_type == "movie")
    tv = next(e for e in events if e.content_type == "tv")
    assert movie.watched_duration == timedelta(minutes=100)
    assert tv.watched_duration == timedelta(minutes=30)
    assert movie.timestamp == datetime(2025, 1, 15)
