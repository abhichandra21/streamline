import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

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


def test_create_client_reads_configured_api_key_name_only_from_environment(monkeypatch):
    import config
    from recommender.llm import create_client

    original_models = config.LLM_MODELS
    monkeypatch.setattr(
        config,
        "LLM_MODELS",
        {
            **original_models,
            "anthropic": {
                **original_models["anthropic"],
                "api_key_env": "WATCH_REGION",
            },
        },
    )
    monkeypatch.delenv("WATCH_REGION", raising=False)

    with pytest.raises(RuntimeError, match="WATCH_REGION not set"):
        create_client("anthropic")


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


def test_load_context_returns_lazy_context_regardless_of_platform_paths(monkeypatch, tmp_path):
    """load_context no longer parses zips; platform paths are ignored at runtime."""
    import config
    import json
    from recommender import main as main_module

    index_path = tmp_path / 'watch_index.json'
    index_path.write_text(json.dumps([{"tmdb_id": 0, "title": "downton abbey", "content_type": "tv"}]))
    profile_path = tmp_path / 'profile.txt'
    profile_path.write_text('taste profile')
    db_path = str(tmp_path / 'streamline.db')

    monkeypatch.setattr(config, 'PLATFORM_PATHS', {'netflix': ['/tmp/missing.zip'], 'prime': [], 'apple_tv': []})
    monkeypatch.setattr(config, 'MANUAL_TV_PATH', None)
    monkeypatch.setattr(config, 'MANUAL_MOVIES_PATH', None)
    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(index_path))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(profile_path))
    monkeypatch.setattr(config, 'TMDB_API_KEY', 'tmdb')
    monkeypatch.setattr(config, 'EVENT_DB_PATH', db_path)
    monkeypatch.setattr(main_module, 'create_client', lambda _provider=None: object())

    ctx = main_module.load_context()

    # Events are lazy — no zip was parsed, no DB exists yet, so load returns []
    assert ctx.events == []
    # _events_resolved is None until first access (lazy)
    # (already resolved above by ctx.events call, so check _events_loader was set)
    assert ctx._events_loader is not None


def test_load_context_fallback_does_not_recreate_db(monkeypatch, tmp_path):
    import config
    import json
    from datetime import datetime, timedelta
    from recommender import main as main_module
    import recommender.setup as setup_module
    from recommender.ingestion.base import WatchEvent

    index_path = tmp_path / "watch_index.json"
    index_path.write_text(json.dumps([{"tmdb_id": 0, "title": "ted lasso", "content_type": "tv"}]))
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text("taste profile")
    db_path = tmp_path / "streamline.db"

    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "WATCH_INDEX_PATH", str(index_path))
    monkeypatch.setattr(config, "TASTE_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(config, "TMDB_API_KEY", "tmdb")
    monkeypatch.setattr(config, "EVENT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "create_client", lambda _provider=None: object())
    monkeypatch.setattr(
        setup_module,
        "_PLATFORM_PARSERS",
        [("netflix", lambda _path: [
            WatchEvent(
                platform="netflix",
                title="Ted Lasso S1E1",
                content_type="tv",
                series_name="Ted Lasso",
                watched_duration=timedelta(hours=1),
                total_duration=None,
                timestamp=datetime(2026, 1, 1),
                profile="user",
            )
        ])],
    )
    monkeypatch.setattr(setup_module, "_compute_file_sha256", lambda _path: "ignored")

    ctx = main_module.load_context()

    assert [event.series_name for event in ctx.events] == ["Ted Lasso"]
    assert not db_path.exists()


def test_resolve_platform_path_uses_default_when_key_missing(monkeypatch):
    import config

    monkeypatch.setattr(config, '_paths', {})

    resolved = config._resolve_platform_paths('netflix', 'data/netflix/export.zip')

    assert resolved == [str(config._ROOT / 'data/netflix/export.zip')]


def test_resolve_platform_path_allows_explicit_disable(monkeypatch):
    import config

    monkeypatch.setattr(config, '_paths', {'netflix': None})
    assert config._resolve_platform_paths('netflix', 'data/netflix/export.zip') == []

    monkeypatch.setattr(config, '_paths', {'netflix': ''})
    assert config._resolve_platform_paths('netflix', 'data/netflix/export.zip') == []


def test_merge_dicts_applies_local_overrides_recursively():
    import config

    base = {
        "platform_paths": {"netflix": None, "prime": None},
        "models": {"openai": {"fast": "base-fast"}},
    }
    local = {
        "platform_paths": {"netflix": "data/netflix/export.zip"},
        "models": {"openai": {"reason": "local-reason"}},
    }

    merged = config._merge_dicts(base, local)

    assert merged["platform_paths"]["netflix"] == "data/netflix/export.zip"
    assert merged["platform_paths"]["prime"] is None
    assert merged["models"]["openai"]["fast"] == "base-fast"
    assert merged["models"]["openai"]["reason"] == "local-reason"


def test_run_ingest_only_exits_if_no_provider_zips_are_configured(monkeypatch, capsys):
    import config
    import recommender.setup as setup

    monkeypatch.setattr(config, 'PLATFORM_PATHS', {'netflix': [], 'prime': [], 'apple_tv': []})
    monkeypatch.setattr(config, 'MANUAL_TV_PATH', 'manual-tv.csv')
    monkeypatch.setattr(config, 'MANUAL_MOVIES_PATH', 'manual-movies.csv')
    monkeypatch.setattr(setup, 'parse_manual', lambda *_args: [object()])

    with pytest.raises(SystemExit) as exc_info:
        setup.run_ingest_only()

    assert exc_info.value.code == 1
    assert 'No providers configured' in capsys.readouterr().err


def test_web_build_context_uses_lazy_events_loader(monkeypatch, tmp_path):
    """_build_context no longer parses zips; it sets a lazy events loader from the DB."""
    import config
    from recommender import web
    import json

    index_path = tmp_path / 'watch_index.json'
    index_path.write_text(json.dumps([{"tmdb_id": 0, "title": "downton abbey", "content_type": "tv"}]))
    profile_path = tmp_path / 'profile.txt'
    profile_path.write_text('taste profile')

    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(index_path))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(profile_path))
    monkeypatch.setattr(config, 'TMDB_API_KEY', 'tmdb')
    monkeypatch.setattr(config, 'EVENT_DB_PATH', str(tmp_path / 'events.db'))
    monkeypatch.setattr(web, 'create_client', lambda: object())
    monkeypatch.setattr(web, 'load_events', lambda _path: [])

    ctx = web._build_context()

    # No zip parsing; context is built cleanly with a lazy events loader
    assert ctx is not None
    assert ctx._events_loader is not None


def test_run_ingest_only_accepts_empty_provider_export(monkeypatch, capsys, tmp_path):
    import config
    import recommender.setup as setup

    monkeypatch.setattr(config, 'PLATFORM_PATHS', {'netflix': ['/tmp/export.zip'], 'prime': [], 'apple_tv': []})
    monkeypatch.setattr(config, 'MANUAL_TV_PATH', None)
    monkeypatch.setattr(config, 'MANUAL_MOVIES_PATH', None)
    monkeypatch.setattr(config, 'EVENT_DB_PATH', str(tmp_path / 'streamline.db'))
    monkeypatch.setattr(setup, '_PLATFORM_PARSERS', [('netflix', lambda _path: [])])
    monkeypatch.setattr(setup, '_compute_file_sha256', lambda _path: 'fakesha256')

    setup.run_ingest_only()

    assert 'netflix:' in capsys.readouterr().err


def test_apple_tv_parse_reports_invalid_zip(tmp_path):
    from recommender.ingestion.apple_tv import parse

    bad_zip = tmp_path / 'bad.zip'
    bad_zip.write_text('not a zip archive')

    with pytest.raises(ValueError, match='Invalid zip file'):
        parse(str(bad_zip))


def _settings_test_client(tmp_path, monkeypatch, raw_cfg: dict | None = None):
    from recommender import web

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw_cfg or {}, sort_keys=False))

    monkeypatch.setattr(web, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(web, "_reload_app_config", lambda: None)
    web.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = web.app.test_client()
    # Set up a CSRF token in the session
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    return client, config_path, web


def _settings_form_data(web, raw_cfg: dict | None = None, **overrides):
    cfg = web._resolve_settings_config(raw_cfg or {})
    data = {
        "provider": cfg["provider"],
        "anthropic_fast": cfg["models"]["anthropic"]["fast"],
        "anthropic_reason": cfg["models"]["anthropic"]["reason"],
        "anthropic_api_key_env": cfg["models"]["anthropic"].get("api_key_env", ""),
        "gemini_fast": cfg["models"]["gemini"]["fast"],
        "gemini_reason": cfg["models"]["gemini"]["reason"],
        "gemini_api_key_env": cfg["models"]["gemini"].get("api_key_env", ""),
        "openai_fast": cfg["models"]["openai"]["fast"],
        "openai_reason": cfg["models"]["openai"]["reason"],
        "openai_api_key_env": cfg["models"]["openai"].get("api_key_env", ""),
        "openai_base_url": cfg["models"]["openai"].get("base_url") or "",
        "llm_timeout_fast": str(cfg["llm"]["timeout_fast"]),
        "llm_timeout_reason": str(cfg["llm"]["timeout_reason"]),
        "llm_timeout_profile_batch": str(cfg["llm"]["timeout_profile_batch"]),
        "llm_timeout_profile_merge": str(cfg["llm"]["timeout_profile_merge"]),
        "llm_tokens_fast": str(cfg["llm"]["tokens_fast"]),
        "llm_tokens_intent": str(cfg["llm"]["tokens_intent"]),
        "llm_tokens_ranking": str(cfg["llm"]["tokens_ranking"]),
        "llm_tokens_suggestions": str(cfg["llm"]["tokens_suggestions"]),
        "llm_tokens_profile_batch": str(cfg["llm"]["tokens_profile_batch"]),
        "llm_tokens_profile_merge": str(cfg["llm"]["tokens_profile_merge"]),
        "llm_tokens_abandoned": str(cfg["llm"]["tokens_abandoned"]),
        "llm_profile_batch_size": str(cfg["llm"]["profile_batch_size"]),
        "llm_rate_limit_wait": str(cfg["llm"]["rate_limit_wait"]),
        "weight_completion": str(cfg["scoring"]["weight_completion"]),
        "weight_rewatch": str(cfg["scoring"]["weight_rewatch"]),
        "weight_recency": str(cfg["scoring"]["weight_recency"]),
        "default_tv_runtime": str(cfg["scoring"]["default_tv_runtime"]),
        "default_movie_runtime": str(cfg["scoring"]["default_movie_runtime"]),
        "rewatch_saturation": str(cfg["scoring"]["rewatch_saturation"]),
        "default_top_n": str(cfg["default_top_n"]),
        "min_vote_count": str(cfg["min_vote_count"]),
        "recency_half_life_days": str(cfg["recency_half_life_days"]),
        "watch_region": cfg["watch_region"],
        "streaming_platforms": ", ".join(cfg["streaming_platforms"]),
        "manual_timestamp_mode": "now" if cfg["manual"]["timestamp"] == "now" else "fixed",
        "manual_timestamp_date": "2022-01-01" if cfg["manual"]["timestamp"] == "now" else cfg["manual"]["timestamp"],
        "manual_tv_duration": str(cfg["manual"]["tv_duration_minutes"]),
        "manual_movie_duration": str(cfg["manual"]["movie_duration_minutes"]),
        "log_level": cfg["log_level"],
    }
    data["_csrf_token"] = "test-csrf-token"
    data.update(overrides)
    return data


def test_settings_page_populates_runtime_defaults_and_accepts_blank_numeric_fields(tmp_path, monkeypatch):
    raw_cfg = {"provider": "gemini"}
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="llm_timeout_fast" value="30"' in body
    assert 'name="default_tv_runtime" value="45"' in body
    assert 'name="default_top_n" value="3"' in body
    assert 'name="recency_half_life_days" value="90"' in body

    post_response = client.post(
        "/settings",
        data=_settings_form_data(
            web,
            raw_cfg,
            llm_timeout_fast="",
            weight_completion="",
            default_tv_runtime="",
            default_top_n="",
            recency_half_life_days="",
            manual_tv_duration="",
        ),
    )

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert saved_cfg["llm"]["timeout_fast"] == 30
    assert saved_cfg["scoring"]["weight_completion"] == 0.5
    assert saved_cfg["scoring"]["default_tv_runtime"] == 45
    assert saved_cfg["default_top_n"] == 3
    assert saved_cfg["recency_half_life_days"] == 90
    assert saved_cfg["manual"]["tv_duration_minutes"] == 45


def test_settings_save_preserves_custom_manual_timestamp(tmp_path, monkeypatch):
    raw_cfg = {"manual": {"timestamp": "2024-07-01"}}
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="manual_timestamp_date"' in body
    assert 'value="2024-07-01"' in body

    post_response = client.post(
        "/settings",
        data=_settings_form_data(web, raw_cfg, log_level="DEBUG"),
    )

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert saved_cfg["manual"]["timestamp"] == "2024-07-01"


def test_settings_save_shows_rebuild_command_after_scoring_change(tmp_path, monkeypatch):
    raw_cfg = {"provider": "anthropic"}
    client, _, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post(
        "/settings",
        data=_settings_form_data(
            web,
            raw_cfg,
            weight_completion="0.6",
            weight_rewatch="0.2",
            weight_recency="0.2",
        ),
    )

    assert post_response.status_code == 302
    location = post_response.headers["Location"]
    assert "saved=1" in location
    assert "rebuild_command" in location
    assert "refresh-profile" in location


def test_settings_page_shows_default_api_key_env_as_placeholder(tmp_path, monkeypatch):
    raw_cfg = {"provider": "gemini"}
    client, _, _ = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="gemini_api_key_env"' in body
    assert 'placeholder="GEMINI_API_KEY"' in body
    assert 'value="GEMINI_API_KEY"' not in body


def test_settings_page_renders_provider_panels_for_client_side_switching(tmp_path, monkeypatch):
    raw_cfg = {"provider": "anthropic"}
    client, _, _ = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-provider-panel="anthropic"' in body
    assert 'data-provider-panel="gemini"' in body
    assert 'data-provider-panel="openai"' in body


def test_settings_save_omits_default_api_key_env_names(tmp_path, monkeypatch):
    raw_cfg = {
        "provider": "anthropic",
        "models": {
            "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
            "gemini": {"api_key_env": "GEMINI_API_KEY"},
            "openai": {"api_key_env": "OPENAI_API_KEY"},
        },
    }
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post("/settings", data=_settings_form_data(web, raw_cfg))

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert "api_key_env" not in saved_cfg["models"]["anthropic"]
    assert "api_key_env" not in saved_cfg["models"]["gemini"]
    assert "api_key_env" not in saved_cfg["models"]["openai"]


def test_settings_save_preserves_custom_api_key_env_override(tmp_path, monkeypatch):
    raw_cfg = {
        "provider": "openai",
        "models": {
            "openai": {
                "fast": "gpt-4.1-mini",
                "reason": "gpt-4.1",
                "api_key_env": "STREAMLINE_OPENAI_KEY",
            },
        },
    }
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post("/settings", data=_settings_form_data(web, raw_cfg))

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert saved_cfg["models"]["openai"]["api_key_env"] == "STREAMLINE_OPENAI_KEY"


def test_settings_save_clears_custom_api_key_env_override(tmp_path, monkeypatch):
    raw_cfg = {
        "provider": "openai",
        "models": {
            "openai": {
                "fast": "gpt-4.1-mini",
                "reason": "gpt-4.1",
                "api_key_env": "STREAMLINE_OPENAI_KEY",
            },
        },
    }
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post(
        "/settings",
        data=_settings_form_data(web, raw_cfg, openai_api_key_env=""),
    )

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert "api_key_env" not in saved_cfg["models"]["openai"]


def test_settings_save_preserves_explicit_platform_path_disables(tmp_path, monkeypatch):
    raw_cfg = {
        "platform_paths": {
            "netflix": None,
            "prime": "",
            "apple_tv": "data/AppleTV/export.zip",
        }
    }
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post("/settings", data=_settings_form_data(web, raw_cfg))

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert saved_cfg["platform_paths"] == {
        "netflix": None,
        "prime": "",
        "apple_tv": "data/AppleTV/export.zip",
    }


def test_settings_save_preserves_local_model_token_controls(tmp_path, monkeypatch):
    raw_cfg = {
        "models": {
            "local": {
                "fast": "gpt-oss:120b",
                "reason": "gpt-oss:120b",
                "base_url": "http://192.168.1.75:11434/v1",
                "thinking": True,
                "thinking_token_scale": 1,
                "thinking_token_floor": 0,
            }
        }
    }
    client, config_path, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post("/settings", data=_settings_form_data(web, raw_cfg))

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert saved_cfg["models"]["local"] == raw_cfg["models"]["local"]


def test_web_module_initializes_logging_on_import():
    import recommender.log as log_module
    import recommender.web as web_module

    original_setup_logging = log_module.setup_logging
    calls: list[str | None] = []

    def fake_setup_logging(level_override: str | None = None) -> None:
        calls.append(level_override)

    try:
        log_module.setup_logging = fake_setup_logging
        importlib.reload(web_module)
        assert calls == [None]
    finally:
        log_module.setup_logging = original_setup_logging
        importlib.reload(web_module)


def test_recommend_web_uses_runtime_provider_for_api_key_check(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parent.parent
    script_path = tmp_path / "recommend-web"
    shutil.copy(repo_root / "recommend-web", script_path)

    activate_path = tmp_path / ".venv" / "bin"
    activate_path.mkdir(parents=True)
    current_python_bin = Path(sys.executable).parent
    (activate_path / "activate").write_text(
        f'export PATH="{current_python_bin}:$PATH"\n'
        f'export VIRTUAL_ENV="{current_python_bin.parent}"\n'
    )

    cfg = yaml.safe_load((repo_root / "config.yaml").read_text())
    cfg["provider"] = "openai"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

    env = os.environ.copy()
    env["STREAMLINE_PORT"] = "5157"
    env["TMDB_API_KEY"] = "tmdb"
    env["ANTHROPIC_API_KEY"] = "anthropic"
    env["LLM_PROVIDER"] = "anthropic"
    env.pop("OPENAI_API_KEY", None)

    result = subprocess.run(
        ["bash", str(script_path), "start"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "taste profile not found" in result.stderr
    assert "OPENAI_API_KEY" not in result.stderr


# ── 3b: apple_tv key in PLATFORM_PATHS ────────────────────────────────────

def test_apple_tv_key_present_in_platform_paths():
    import config
    assert "apple_tv" in config.PLATFORM_PATHS


# ── 3c: _resolve_platform_path with default=None ─────────────────────────

def test_resolve_platform_path_with_none_default_returns_none_when_key_missing(monkeypatch):
    import config
    monkeypatch.setattr(config, "_paths", {})
    assert config._resolve_platform_paths("apple_tv", None) == []


# ── 3d: config.local.yaml overrides config.yaml when both present ─────────

def test_local_config_overrides_base_config_when_both_files_present(tmp_path):
    import config

    base_file = tmp_path / "config.yaml"
    base_file.write_text(
        "provider: anthropic\nplatform_paths:\n  netflix: data/netflix/export.zip\n"
    )
    local_file = tmp_path / "config.local.yaml"
    local_file.write_text(
        "platform_paths:\n  apple_tv: /path/to/apple_export.zip\n"
    )

    base = config._load_yaml(base_file)
    local = config._load_yaml(local_file)
    merged = config._merge_dicts(base, local)

    assert merged["provider"] == "anthropic"
    assert merged["platform_paths"]["netflix"] == "data/netflix/export.zip"
    assert merged["platform_paths"]["apple_tv"] == "/path/to/apple_export.zip"


# ── 4c: Mixed: one provider fails, one succeeds -> strict mode exit 1 ──────

def test_run_ingest_only_exits_if_one_provider_fails_in_strict_mode(monkeypatch, capsys):
    import config
    import recommender.setup as setup
    from datetime import datetime, timedelta
    from recommender.ingestion.base import WatchEvent

    def _ok_parser(_path):
        return [WatchEvent(
            platform="netflix",
            title="Succession",
            content_type="tv",
            series_name="Succession",
            watched_duration=timedelta(hours=1),
            total_duration=None,
            timestamp=datetime(2026, 1, 1),
            profile="",
        )]

    def _fail_parser(_path):
        raise ValueError("corrupt zip")

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": "/tmp/netflix.zip",
        "prime": "/tmp/prime.zip",
        "apple_tv": None,
    })
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [
        ("netflix", _ok_parser),
        ("prime", _fail_parser),
    ])

    with pytest.raises(SystemExit) as exc_info:
        setup.run_ingest_only()

    assert exc_info.value.code == 1
    assert "FAIL" in capsys.readouterr().err


# ── 4d: All providers valid with events -> prints TV/movie breakdown ────────

def test_run_ingest_only_prints_tv_and_movie_summary(monkeypatch, capsys, tmp_path):
    import config
    import recommender.setup as setup
    from datetime import datetime, timedelta
    from recommender.ingestion.base import WatchEvent

    def _parser(_path):
        return [
            WatchEvent(
                platform="netflix",
                title="Succession: Season 1: Episode 1 (Episode 1)",
                content_type="tv",
                series_name="Succession",
                watched_duration=timedelta(hours=1),
                total_duration=None,
                timestamp=datetime(2026, 1, 1),
                profile="",
            ),
            WatchEvent(
                platform="netflix",
                title="Inception",
                content_type="movie",
                series_name="Inception",
                watched_duration=timedelta(hours=2),
                total_duration=None,
                timestamp=datetime(2026, 1, 2),
                profile="",
            ),
        ]

    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "EVENT_DB_PATH", str(tmp_path / "streamline.db"))
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", _parser)])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "fakesha256")

    setup.run_ingest_only()

    err = capsys.readouterr().err
    assert "TV shows" in err
    assert "movies" in err


# ── 4e: run_setup() with zero total events -> exit 1 ──────────────────────

def test_run_setup_exits_with_no_watch_events_message(monkeypatch, capsys, tmp_path):
    import config
    import recommender.setup as setup

    db_path = str(tmp_path / "streamline.db")
    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)
    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-tmdb-key")
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", lambda _path: [])])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "fakesha256")
    class _FakeLLM:
        provider = "anthropic"
    monkeypatch.setattr(setup, "create_client", lambda _provider=None: _FakeLLM())

    with pytest.raises(SystemExit) as exc_info:
        setup.run_setup()

    assert exc_info.value.code == 1
    assert "No watch events found" in capsys.readouterr().err


# ── 5c: Ordinary recommendation flow still works after provider zips removed

def test_load_context_works_with_no_configured_providers(monkeypatch, tmp_path):
    """After provider zips are removed, load_context returns cached index/profile."""
    import config
    import json
    from recommender import main as main_module

    index_path = tmp_path / "watch_index.json"
    index_path.write_text(json.dumps([{"tmdb_id": 1, "title": "succession", "content_type": "tv"}]))
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text("taste profile")
    db_path = tmp_path / "streamline.db"

    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": [], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "WATCH_INDEX_PATH", str(index_path))
    monkeypatch.setattr(config, "TASTE_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(config, "TMDB_API_KEY", "tmdb")
    monkeypatch.setattr(config, "EVENT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "create_client", lambda _provider=None: object())

    ctx = main_module.load_context()

    assert ctx.events == []
    assert ctx.taste_profile == "taste profile"


def test_event_db_path_defaults_to_data_streamline_db():
    import config
    assert config.EVENT_DB_PATH.endswith("data/streamline.db")


def test_refresh_data_reingests_configured_exports(monkeypatch, capsys, tmp_path):
    """--refresh-data should parse current exports and persist the refreshed event set."""
    import config
    import recommender.setup as setup
    from recommender.event_store import init_db, load_events, replace_provider_events
    from datetime import datetime, timedelta
    from recommender.ingestion.base import WatchEvent

    db_path = str(tmp_path / "streamline.db")
    init_db(db_path)
    replace_provider_events(db_path, "netflix", [
        WatchEvent(platform="netflix", title="Succession S1E1", content_type="tv",
                   series_name="Succession", watched_duration=timedelta(hours=1),
                   total_duration=None, timestamp=datetime(2026, 1, 1), profile="user"),
    ], [{"path": "/export.zip", "sha256": "sha1"}], "sha1")

    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)
    # Provide a non-None path so the current zip loop WOULD call the parser (making pre-fix test fail)
    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/netflix_export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-tmdb-key")

    # Track that the current export is parsed during refresh.
    parser_called = False
    def _spy_parser(_path):
        nonlocal parser_called
        parser_called = True
        return [
            WatchEvent(
                platform="netflix",
                title="The Bear S1E1",
                content_type="tv",
                series_name="The Bear",
                watched_duration=timedelta(hours=1),
                total_duration=None,
                timestamp=datetime(2026, 2, 1),
                profile="user",
            ),
        ]
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", _spy_parser)])

    class _FakeLLM:
        provider = "anthropic"
    monkeypatch.setattr(setup, "create_client", lambda _provider=None: _FakeLLM())

    # Mock TMDB and downstream to avoid real API calls
    mock_tmdb = MagicMock()
    mock_tmdb.get_metadata.return_value = None
    monkeypatch.setattr(setup, "TmdbClient", lambda **_kw: mock_tmdb)

    index_path = tmp_path / "watch_index.json"
    monkeypatch.setattr(config, "WATCH_INDEX_PATH", str(index_path))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "ENRICHMENT_CACHE_DIR", str(tmp_path / "enrichments"))
    monkeypatch.setattr(config, "PROVIDERS_CACHE_DIR", str(tmp_path / "providers"))
    monkeypatch.setattr(config, "OVERRIDES_PATH", str(tmp_path / "overrides.json"))
    monkeypatch.setattr(setup, "enrich_batch", lambda *_a, **_kw: {})
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "freshsha")
    monkeypatch.setattr(config, "TASTE_PROFILE_PATH", str(tmp_path / "profile.txt"))

    # Run refresh_data - parser should be called and the refreshed event set should reach SQLite.
    try:
        setup.run_setup(refresh_data=True)
    except (SystemExit, Exception):
        pass  # Downstream build steps are not fully mocked here.

    refreshed = load_events(db_path, provider="netflix")
    assert parser_called, "Refresh-data should re-parse configured provider exports"
    assert [event.title for event in refreshed] == ["The Bear S1E1"]


def test_run_setup_zero_events_persists_imports_before_exit(monkeypatch, capsys, tmp_path):
    import config
    import recommender.setup as setup
    from recommender.event_store import get_import_info

    db_path = str(tmp_path / "streamline.db")
    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)
    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-tmdb-key")
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", lambda _path: [])])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "fakesha256")

    class _FakeLLM:
        provider = "anthropic"
    monkeypatch.setattr(setup, "create_client", lambda _provider=None: _FakeLLM())

    with pytest.raises(SystemExit) as exc_info:
        setup.run_setup()

    assert exc_info.value.code == 1
    assert "No watch events found" in capsys.readouterr().err

    # Even though setup exited, zero-event import should be in SQLite
    info = get_import_info(db_path)
    assert "netflix" in info
    assert info["netflix"]["event_count"] == 0


def test_load_context_does_not_parse_zips(monkeypatch, tmp_path):
    """load_context should not call any provider parsers."""
    import config
    import json
    from recommender import main as main_module

    index_path = tmp_path / "watch_index.json"
    index_path.write_text(json.dumps([{"tmdb_id": 1, "title": "test", "content_type": "tv"}]))
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text("taste profile")
    db_path = str(tmp_path / "streamline.db")

    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "WATCH_INDEX_PATH", str(index_path))
    monkeypatch.setattr(config, "TASTE_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(config, "TMDB_API_KEY", "tmdb")
    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)
    monkeypatch.setattr(main_module, "create_client", lambda _provider=None: object())

    # Should not import or call any parser
    ctx = main_module.load_context()

    # events should be lazy (empty because no SQLite DB)
    assert ctx.events == []
    # The key assertion: no zip parsing happened (would have raised FileNotFoundError
    # for /tmp/export.zip if parsers were called)


def test_load_context_debug_log_does_not_force_events_load(monkeypatch, tmp_path):
    """Debug log should not access ctx.events (would force lazy load)."""
    import config
    import json
    from recommender import main as main_module

    index_path = tmp_path / "watch_index.json"
    index_path.write_text(json.dumps([{"tmdb_id": 1, "title": "test", "content_type": "tv"}]))
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text("taste profile")
    db_path = str(tmp_path / "streamline.db")

    monkeypatch.setattr(config, "PLATFORM_PATHS", {"netflix": [], "prime": [], "apple_tv": []})
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "WATCH_INDEX_PATH", str(index_path))
    monkeypatch.setattr(config, "TASTE_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(config, "TMDB_API_KEY", "tmdb")
    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)

    mock_llm = MagicMock()
    monkeypatch.setattr(main_module, "create_client", lambda _provider=None: mock_llm)

    ctx = main_module.load_context()
    # After load_context, _events_resolved should still be None (not loaded)
    assert ctx._events_resolved is None


def test_liked_writes_to_sqlite(tmp_path, monkeypatch):
    from recommender.user_store import init_db, load_ratings
    import recommender.user_store as us

    db = str(tmp_path / "test.db")
    init_db(db)
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr(us, "resolve_rating_content_type", lambda *_args, **_kwargs: "tv")
    monkeypatch.setattr("sys.argv", ["recommend", "--liked", "Breaking Bad"])
    monkeypatch.setattr("config.WATCH_INDEX_PATH", str(tmp_path / "wi.json"))
    monkeypatch.setattr("config.TASTE_PROFILE_PATH", str(tmp_path / "tp.txt"))

    from recommender.main import main
    main()

    ratings = load_ratings(db)
    assert any(r["title"] == "Breaking Bad" and r["rating"] == "liked" for r in ratings)


def test_add_writes_to_sqlite(tmp_path, monkeypatch):
    from recommender.user_store import init_db, list_manual_archive

    db = str(tmp_path / "test.db")
    init_db(db)
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr("sys.argv", ["recommend", "--add", "The Bear", "--type", "tv"])
    monkeypatch.setattr("config.WATCH_INDEX_PATH", str(tmp_path / "wi.json"))
    monkeypatch.setattr("config.TASTE_PROFILE_PATH", str(tmp_path / "tp.txt"))

    from recommender.main import main
    main()

    archive = list_manual_archive(db)
    assert any(a["title"] == "The Bear" for a in archive)


def test_cache_audit_reports_existing_cache_with_mismatched_title(tmp_path, capsys):
    import json
    import recommender.setup as setup
    from recommender.watch_index import WatchIndex

    cache_path = tmp_path / "tv" / "55063.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "id": 55063,
        "name": "Man on the Moon: The Epic Journey of Apollo 11",
    }))
    index = WatchIndex(
        tmdb_ids={55063},
        tmdb_keys={("tv", 55063)},
        normalized_titles={("apollo 11", "tv")},
        entries=[{"tmdb_id": 55063, "title": "Apollo 11", "content_type": "tv"}],
    )

    setup._audit_cache_mismatches(index, str(tmp_path), audit_output_path=str(tmp_path / "audit.txt"))

    err = capsys.readouterr().err
    assert "Apollo 11" in err
    assert "Man on the Moon" in err


def test_cache_audit_allows_exact_short_title_match(tmp_path, capsys):
    import json
    import recommender.setup as setup
    from recommender.watch_index import WatchIndex

    cache_path = tmp_path / "movie" / "11.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "id": 11,
        "title": "Up",
        "release_date": "2009-05-28",
    }))
    index = WatchIndex(
        tmdb_ids={11},
        tmdb_keys={("movie", 11)},
        normalized_titles={("up", "movie")},
        entries=[{"tmdb_id": 11, "title": "Up", "content_type": "movie"}],
    )

    setup._audit_cache_mismatches(index, str(tmp_path), audit_output_path=str(tmp_path / "audit.txt"))

    err = capsys.readouterr().err
    assert "possible wrong TMDB match" not in err
    assert "title mismatch" not in err


def test_build_hints_map_manual_year():
    from datetime import datetime, timedelta
    import recommender.setup as setup
    from recommender.ingestion.base import WatchEvent

    event = WatchEvent(
        platform='manual', title='Honeyland', content_type='movie',
        series_name='Honeyland', watched_duration=timedelta(minutes=120),
        total_duration=timedelta(minutes=120), timestamp=datetime.now(),
        profile='', release_year_hint=2019,
    )
    hints_map = setup._build_hints_map([event])
    assert ('Honeyland', 'movie') in hints_map
    assert hints_map[('Honeyland', 'movie')].release_year == 2019


def test_build_hints_map_manual_no_year():
    from datetime import datetime, timedelta
    import recommender.setup as setup
    from recommender.ingestion.base import WatchEvent

    event = WatchEvent(
        platform='manual', title='Some Movie', content_type='movie',
        series_name='Some Movie', watched_duration=timedelta(minutes=120),
        total_duration=timedelta(minutes=120), timestamp=datetime.now(),
        profile='',
    )
    hints_map = setup._build_hints_map([event])
    assert ('Some Movie', 'movie') not in hints_map


def test_build_hints_map_apple_tv_runtime():
    from datetime import datetime, timedelta
    import recommender.setup as setup
    from recommender.ingestion.base import WatchEvent

    event = WatchEvent(
        platform='apple_tv', title='Test Movie', content_type='movie',
        series_name='Test Movie', watched_duration=timedelta(minutes=100),
        total_duration=timedelta(minutes=142), timestamp=datetime.now(),
        profile='testuser',
    )
    hints_map = setup._build_hints_map([event])
    hints = hints_map[('Test Movie', 'movie')]
    assert hints.runtime_minutes == 142
    assert hints.runtime_is_exact is True


def test_build_hints_map_manual_duration_not_used():
    """Manual default durations should not be passed as runtime hints."""
    from datetime import datetime, timedelta
    import recommender.setup as setup
    from recommender.ingestion.base import WatchEvent

    event = WatchEvent(
        platform='manual', title='Generic Movie', content_type='movie',
        series_name='Generic Movie', watched_duration=timedelta(minutes=120),
        total_duration=timedelta(minutes=120), timestamp=datetime.now(),
        profile='',
    )
    hints_map = setup._build_hints_map([event])
    # No year hint and manual platform -> no hints entry
    assert ('Generic Movie', 'movie') not in hints_map


def test_audit_reports_year_mismatch(tmp_path, capsys):
    import json
    import recommender.setup as setup
    from recommender.watch_index import WatchIndex
    from recommender.tmdb_client import MatchHints

    cache_path = tmp_path / "movie" / "111.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "id": 111, "title": "Iceland", "release_date": "1942-01-01",
        "runtime": 90, "vote_count": 50, "popularity": 5, "poster_path": "/p.jpg",
    }))
    index = WatchIndex(
        tmdb_ids={111},
        tmdb_keys={("movie", 111)},
        normalized_titles={("iceland", "movie")},
        entries=[{"tmdb_id": 111, "title": "Iceland", "content_type": "movie"}],
    )
    hints_map = {("Iceland", "movie"): MatchHints(release_year=2016)}

    setup._audit_cache_mismatches(index, str(tmp_path), hints_map, audit_output_path=str(tmp_path / "audit.txt"))

    err = capsys.readouterr().err
    assert "year mismatch" in err.lower() or "source year 2016" in err


def test_audit_reports_runtime_mismatch(tmp_path, capsys):
    import json
    import recommender.setup as setup
    from recommender.watch_index import WatchIndex
    from recommender.tmdb_client import MatchHints

    cache_path = tmp_path / "movie" / "555.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "id": 555, "title": "Hum Tum", "release_date": "2004-05-28",
        "runtime": 80, "vote_count": 100, "popularity": 10, "poster_path": "/p.jpg",
    }))
    index = WatchIndex(
        tmdb_ids={555},
        tmdb_keys={("movie", 555)},
        normalized_titles={("hum tum", "movie")},
        entries=[{"tmdb_id": 555, "title": "Hum Tum", "content_type": "movie"}],
    )
    hints_map = {("Hum Tum", "movie"): MatchHints(runtime_minutes=140, runtime_is_exact=True)}

    setup._audit_cache_mismatches(index, str(tmp_path), hints_map, audit_output_path=str(tmp_path / "audit.txt"))

    err = capsys.readouterr().err
    assert "runtime" in err.lower() or "source 140min" in err


def test_audit_existing_title_mismatch_still_works(tmp_path, capsys):
    """Existing title mismatch audit continues to work."""
    import json
    import recommender.setup as setup
    from recommender.watch_index import WatchIndex

    cache_path = tmp_path / "movie" / "700.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "id": 700, "title": "The King", "release_date": "2019-10-11",
        "runtime": 140, "vote_count": 2000, "popularity": 50, "poster_path": "/p.jpg",
    }))
    index = WatchIndex(
        tmdb_ids={700},
        tmdb_keys={("movie", 700)},
        normalized_titles={("kesari", "movie")},
        entries=[{"tmdb_id": 700, "title": "Kesari", "content_type": "movie"}],
    )

    setup._audit_cache_mismatches(index, str(tmp_path), audit_output_path=str(tmp_path / "audit.txt"))

    err = capsys.readouterr().err
    assert "Kesari" in err
    assert "The King" in err
