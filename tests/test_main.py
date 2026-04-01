import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recommender.models import Recommendation


def make_rec(title="Broadchurch", explanation="Great fit."):
    return Recommendation(
        title=title, content_type="tv", score=0.9,
        vote_average=8.4, genres=["Crime", "Drama"],
        explanation=explanation,
    )


def test_print_recommendations_shows_title(capsys):
    from recommender.main import print_recommendations
    results = [make_rec("Broadchurch", "Fits your British crime taste.")]
    print_recommendations(results, "British crime drama")
    out = capsys.readouterr().out
    assert "Broadchurch" in out
    assert "Fits your British crime taste" in out


def test_print_recommendations_empty(capsys):
    from recommender.main import print_recommendations
    print_recommendations([], "query")
    out = capsys.readouterr().out
    assert "No recommendations" in out


def test_main_exits_without_anthropic_key(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['recommender', 'test query'])
    import config
    original = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = ""
    try:
        with pytest.raises(SystemExit) as exc_info:
            from recommender import main as main_module
            import importlib
            importlib.reload(main_module)
            main_module.main()
        assert exc_info.value.code == 1
    finally:
        config.ANTHROPIC_API_KEY = original


def test_load_context_exits_if_no_watch_index(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(tmp_path / 'missing.json'))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(tmp_path / 'profile.txt'))
    monkeypatch.setattr(config, 'PLATFORM_PATHS', {})
    with pytest.raises(SystemExit):
        from recommender.main import load_context
        load_context()


def test_load_context_exits_if_no_taste_profile(tmp_path, monkeypatch):
    import config
    import json
    index_path = tmp_path / 'watch_index.json'
    index_path.write_text(json.dumps([{"tmdb_id": 0, "title": "downton abbey", "content_type": "tv"}]))
    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(index_path))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(tmp_path / 'missing.txt'))
    monkeypatch.setattr(config, 'PLATFORM_PATHS', {})
    with pytest.raises(SystemExit):
        from recommender.main import load_context
        load_context()
