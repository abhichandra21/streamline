import sys
import pytest
from unittest.mock import patch
from recommender.main import load_all_events, print_results, save_csv
from recommender import Recommendation
import tempfile, os


def test_load_all_events_returns_list():
    events = load_all_events()
    assert isinstance(events, list)
    assert len(events) > 0


def test_load_all_events_skips_none_paths(tmp_path):
    import config
    original = config.PLATFORM_PATHS.copy()
    config.PLATFORM_PATHS["prime"] = None
    events = load_all_events()  # should not raise even with None paths
    config.PLATFORM_PATHS.update(original)


def test_print_results_no_crash(capsys):
    recs = [
        Recommendation(
            title="Test Show", content_type="tv", score=0.85,
            vote_average=8.5, genres=["Drama", "Thriller"],
            because_you_watched="Breaking Bad"
        )
    ]
    print_results({"tv": recs})
    captured = capsys.readouterr()
    assert "Test Show" in captured.out
    assert "Breaking Bad" in captured.out


def test_save_csv_writes_file():
    recs = [
        Recommendation(
            title="My Movie", content_type="movie", score=0.75,
            vote_average=7.8, genres=["Action"], because_you_watched="The Dark Knight"
        )
    ]
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        path = f.name
    try:
        save_csv({"movies": recs}, path)
        with open(path) as f:
            content = f.read()
        assert "My Movie" in content
        assert "Action" in content
    finally:
        os.unlink(path)


def test_main_exits_without_api_key(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["recommender"])
    monkeypatch.setenv("TMDB_API_KEY", "")
    import config
    config.TMDB_API_KEY = ""
    with pytest.raises(SystemExit) as exc_info:
        from recommender.main import main
        main()
    assert exc_info.value.code == 1
