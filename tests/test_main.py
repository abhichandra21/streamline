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


def test_resolve_platform_path_uses_default_when_key_missing(monkeypatch):
    import config

    monkeypatch.setattr(config, '_paths', {})

    resolved = config._resolve_platform_path('netflix', 'data/netflix/ViewingActivity.csv')

    assert resolved == str(config._ROOT / 'data/netflix/ViewingActivity.csv')


def test_resolve_platform_path_allows_explicit_disable(monkeypatch):
    import config

    monkeypatch.setattr(config, '_paths', {'netflix': None})
    assert config._resolve_platform_path('netflix', 'data/netflix/ViewingActivity.csv') is None

    monkeypatch.setattr(config, '_paths', {'netflix': ''})
    assert config._resolve_platform_path('netflix', 'data/netflix/ViewingActivity.csv') is None


def _settings_test_client(tmp_path, monkeypatch, raw_cfg: dict | None = None):
    from recommender import web

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw_cfg or {}, sort_keys=False))

    refresh_calls: list[tuple[bool, bool]] = []

    def record_refresh(*, refresh_profile: bool, refresh_data: bool) -> None:
        refresh_calls.append((refresh_profile, refresh_data))

    monkeypatch.setattr(web, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(web, "_reload_app_config", lambda: None)
    monkeypatch.setattr(web, "_refresh_derived_data", record_refresh)
    web.app.config.update(TESTING=True)
    return web.app.test_client(), config_path, refresh_calls, web


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
    data.update(overrides)
    return data


def test_settings_page_populates_runtime_defaults_and_accepts_blank_numeric_fields(tmp_path, monkeypatch):
    raw_cfg = {"provider": "gemini"}
    client, config_path, refresh_calls, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    assert refresh_calls == []


def test_settings_save_preserves_custom_manual_timestamp(tmp_path, monkeypatch):
    raw_cfg = {"manual": {"timestamp": "2024-07-01"}}
    client, config_path, refresh_calls, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    assert refresh_calls == []


def test_settings_save_rebuilds_profile_after_setup_only_change(tmp_path, monkeypatch):
    raw_cfg = {"provider": "anthropic"}
    client, _, refresh_calls, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    assert refresh_calls == [(True, False)]


def test_settings_page_shows_default_api_key_env_as_placeholder(tmp_path, monkeypatch):
    raw_cfg = {"provider": "gemini"}
    client, _, _, _ = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="gemini_api_key_env"' in body
    assert 'placeholder="GEMINI_API_KEY"' in body
    assert 'value="GEMINI_API_KEY"' not in body


def test_settings_page_renders_provider_panels_for_client_side_switching(tmp_path, monkeypatch):
    raw_cfg = {"provider": "anthropic"}
    client, _, _, _ = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    client, config_path, _, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    client, config_path, _, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

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
    client, config_path, _, web = _settings_test_client(tmp_path, monkeypatch, raw_cfg)

    post_response = client.post(
        "/settings",
        data=_settings_form_data(web, raw_cfg, openai_api_key_env=""),
    )

    assert post_response.status_code == 302
    saved_cfg = yaml.safe_load(config_path.read_text())
    assert "api_key_env" not in saved_cfg["models"]["openai"]


def test_recommend_web_uses_runtime_provider_for_api_key_check(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parent.parent
    script_path = tmp_path / "recommend-web"
    shutil.copy(repo_root / "recommend-web", script_path)

    activate_path = tmp_path / "venv" / "bin"
    activate_path.mkdir(parents=True)
    (activate_path / "activate").write_text("# recommend-web test activation\n")

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
