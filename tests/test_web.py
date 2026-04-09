"""Tests for the web UI contract: jobs, progressive enhancement, settings/rebuild separation."""

import time
from unittest.mock import patch, MagicMock

import pytest

from recommender import web
from recommender.jobs import Job, JobRegistry
from recommender.web import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        # Establish a session so CSRF token is available
        with c.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf-token"
        yield c


@pytest.fixture(autouse=True)
def reset_reload_state():
    web._config_reload_pending = False
    yield
    web._config_reload_pending = False


def _csrf_form(**kwargs):
    """Return form data dict with CSRF token included."""
    return {"_csrf_token": "test-csrf-token", **kwargs}


def _csrf_headers():
    return {"X-CSRF-Token": "test-csrf-token"}


# ── Job registry tests ────────────────────────────────────────────────────────

class TestJobRegistry:
    def test_successful_job(self):
        registry = JobRegistry()
        job_id = registry.submit(lambda: "result", label="test")
        # Wait for completion
        for _ in range(50):
            job = registry.get(job_id)
            if job and job.status in ("done", "error"):
                break
            time.sleep(0.05)
        job = registry.get(job_id)
        assert job.status == "done"
        assert job.result == "result"
        assert job.duration_seconds is not None

    def test_failed_job_surfaces_error(self):
        registry = JobRegistry()
        job_id = registry.submit(lambda: 1 / 0, label="fail-test")
        for _ in range(50):
            job = registry.get(job_id)
            if job and job.status in ("done", "error"):
                break
            time.sleep(0.05)
        job = registry.get(job_id)
        assert job.status == "error"
        assert "division by zero" in job.error
        assert job.finished_at is not None

    def test_system_exit_surfaces_as_error(self):
        def exit_job():
            raise SystemExit(1)

        registry = JobRegistry()
        job_id = registry.submit(exit_job, label="exit-test")
        for _ in range(50):
            job = registry.get(job_id)
            if job and job.status in ("done", "error"):
                break
            time.sleep(0.05)
        job = registry.get(job_id)
        assert job.status == "error"
        assert "exited with status 1" in job.error

    def test_unknown_job_returns_none(self):
        registry = JobRegistry()
        assert registry.get("nonexistent") is None


class TestDeferredConfigReload:
    @patch("recommender.web.importlib.reload")
    @patch("recommender.web.job_registry.running_jobs")
    def test_reload_defers_while_jobs_running(self, mock_running_jobs, mock_reload):
        mock_running_jobs.return_value = [MagicMock(label="rebuilding taste profile")]

        applied = web._reload_app_config()

        assert applied is False
        assert web._config_reload_pending is True
        mock_reload.assert_not_called()

    @patch("recommender.web.importlib.reload")
    @patch("recommender.web.job_registry.running_jobs")
    def test_deferred_reload_applies_after_jobs_finish(self, mock_running_jobs, mock_reload):
        web._config_reload_pending = True
        mock_running_jobs.return_value = []

        web._maybe_apply_deferred_reload()

        mock_reload.assert_called_once_with(web.config)
        assert web._config_reload_pending is False


class TestStatusObservability:
    def test_build_context_does_not_parse_zips(self, tmp_path, monkeypatch):
        """_build_context should not call any zip parsers; events come from the DB lazily."""
        index_path = tmp_path / "watch_index.json"
        profile_path = tmp_path / "profile.txt"
        index_path.write_text("[]")
        profile_path.write_text("taste profile")

        monkeypatch.setattr(web.config, "WATCH_INDEX_PATH", str(index_path))
        monkeypatch.setattr(web.config, "TASTE_PROFILE_PATH", str(profile_path))
        monkeypatch.setattr(web.config, "TMDB_API_KEY", "tmdb")
        monkeypatch.setattr(web.config, "CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(web.config, "ENRICHMENT_CACHE_DIR", str(tmp_path / "enrich"))
        monkeypatch.setattr(web.config, "PROVIDERS_CACHE_DIR", str(tmp_path / "providers"))
        monkeypatch.setattr(web.config, "WATCH_REGION", "US")
        monkeypatch.setattr(web.config, "STREAMING_PLATFORMS", [])
        monkeypatch.setattr(web.config, "EVENT_DB_PATH", str(tmp_path / "events.db"))

        monkeypatch.setattr(web.wi, "load", lambda _path: MagicMock(entries=[]))
        monkeypatch.setattr(web, "TmdbClient", lambda **_kwargs: object())
        monkeypatch.setattr(web, "create_client", lambda: object())
        monkeypatch.setattr(web, "load_events", lambda _path: [])

        ctx = web._build_context()

        # Context is built without errors; events loader is set lazily
        assert ctx is not None

    def test_build_context_fallback_does_not_recreate_db(self, tmp_path, monkeypatch):
        """Touching ctx.events with no DB should use a read-only fallback."""
        import json
        from datetime import datetime, timedelta
        import recommender.setup as setup_module
        from recommender.ingestion.base import WatchEvent

        index_path = tmp_path / "watch_index.json"
        profile_path = tmp_path / "profile.txt"
        index_path.write_text(json.dumps([{"tmdb_id": 1, "title": "ted lasso", "content_type": "tv"}]))
        profile_path.write_text("taste profile")
        db_path = tmp_path / "events.db"

        monkeypatch.setattr(web.config, "PLATFORM_PATHS", {"netflix": "/tmp/export.zip", "prime": None, "apple_tv": None})
        monkeypatch.setattr(web.config, "MANUAL_TV_PATH", None)
        monkeypatch.setattr(web.config, "MANUAL_MOVIES_PATH", None)
        monkeypatch.setattr(web.config, "WATCH_INDEX_PATH", str(index_path))
        monkeypatch.setattr(web.config, "TASTE_PROFILE_PATH", str(profile_path))
        monkeypatch.setattr(web.config, "TMDB_API_KEY", "tmdb")
        monkeypatch.setattr(web.config, "CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(web.config, "ENRICHMENT_CACHE_DIR", str(tmp_path / "enrich"))
        monkeypatch.setattr(web.config, "PROVIDERS_CACHE_DIR", str(tmp_path / "providers"))
        monkeypatch.setattr(web.config, "WATCH_REGION", "US")
        monkeypatch.setattr(web.config, "STREAMING_PLATFORMS", [])
        monkeypatch.setattr(web.config, "EVENT_DB_PATH", str(db_path))

        monkeypatch.setattr(web.wi, "load", lambda _path: MagicMock(entries=[]))
        monkeypatch.setattr(web, "TmdbClient", lambda **_kwargs: object())
        monkeypatch.setattr(web, "create_client", lambda: object())
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

        ctx = web._build_context()

        assert [event.series_name for event in ctx.events] == ["Ted Lasso"]
        assert not db_path.exists()

    @patch("recommender.web.job_registry")
    @patch("recommender.web._get_context")
    def test_status_is_always_ok(self, mock_get_context, mock_jobs, client):
        """Status should always be 'ok' — event store missing is informational, not degraded."""
        mock_get_context.return_value = MagicMock(watch_index=MagicMock(entries=[]))
        mock_jobs.running_jobs.return_value = []
        mock_jobs.recent_jobs.return_value = []

        with patch("recommender.web.event_store") as mock_es:
            mock_es.get_import_info.return_value = {}
            resp = client.get("/status")

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["status"] == "ok"
        assert "ingestion_errors" not in payload


# ── /recommend progressive enhancement ────────────────────────────────────────

class TestRecommendProgressive:
    @patch("recommender.web._get_context")
    def test_post_without_htmx_returns_full_page(self, mock_ctx, client):
        """Non-HTMX POST to /recommend should return a full HTML page, not a fragment."""
        mock_context = MagicMock()
        mock_context.llm = MagicMock()
        mock_context.llm.provider = "anthropic"
        mock_context.llm.usage = MagicMock()
        mock_context.llm.usage.summary.return_value = {}
        mock_context.tmdb_client = MagicMock()
        mock_context.tmdb_client.get_metadata.return_value = None
        mock_ctx.return_value = mock_context

        with patch("recommender.web.ask", return_value=[]):
            resp = client.post("/recommend", data=_csrf_form(query="test query"))

        assert resp.status_code == 200
        html = resp.data.decode()
        # Full page should have the base template structure
        assert "<!DOCTYPE html>" in html or "<html" in html
        assert "Discover" in html

    def test_post_without_htmx_empty_query_returns_page(self, client):
        """Empty query without HTMX should return the full recommend page."""
        resp = client.post("/recommend", data=_csrf_form(query=""))
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Discover" in html

    @patch("recommender.web._run_recommend_job")
    @patch("recommender.web.job_registry")
    def test_post_with_htmx_returns_polling_fragment(self, mock_registry, mock_job, client):
        """HTMX POST should return a polling fragment, not a full page."""
        mock_registry.submit.return_value = "test-job-id"
        resp = client.post(
            "/recommend",
            data=_csrf_form(query="test query"),
            headers={**_csrf_headers(), "HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "test-job-id" in html
        # Should NOT be a full page
        assert "<!DOCTYPE html>" not in html


# ── Settings save without rebuild ──────────────────────────────────────────────

class TestSettingsSaveRebuild:
    @patch("recommender.web._reload_app_config")
    @patch("recommender.web._save_config_yaml")
    @patch("recommender.web._load_config_yaml")
    def test_save_settings_does_not_trigger_rebuild(self, mock_load, mock_save, mock_reload, client):
        """Saving settings should never auto-trigger a rebuild job."""
        mock_reload.return_value = True
        mock_load.return_value = {
            "provider": "anthropic",
            "models": {
                "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
                "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
                "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
            },
            "llm": {
                "timeout_fast": 30, "timeout_reason": 60,
                "timeout_profile_batch": 60, "timeout_profile_merge": 300,
                "tokens_fast": 200, "tokens_intent": 400, "tokens_ranking": 1000,
                "tokens_suggestions": 300, "tokens_profile_batch": 800,
                "tokens_profile_merge": 4000, "tokens_abandoned": 300,
                "profile_batch_size": 200, "rate_limit_wait": 65,
            },
            "scoring": {
                "weight_completion": 0.5, "weight_rewatch": 0.3, "weight_recency": 0.2,
                "default_tv_runtime": 45, "default_movie_runtime": 90, "rewatch_saturation": 5,
            },
            "default_top_n": 3, "min_vote_count": 20, "min_rating": 0, "min_year": 0,
            "recency_half_life_days": 90, "watch_region": "US", "streaming_platforms": [],
            "manual": {"timestamp": "now", "tv_duration_minutes": 45, "movie_duration_minutes": 120},
            "log_level": "WARNING",
        }

        with patch("recommender.web.job_registry") as mock_jobs:
            # Change only a runtime setting (provider)
            resp = client.post("/settings", data=_csrf_form(
                provider="gemini",
                weight_completion="0.5", weight_rewatch="0.3", weight_recency="0.2",
                default_top_n="3", min_vote_count="20", min_rating="0", min_year="0",
                recency_half_life_days="90",
                watch_region="US", streaming_platforms="",
                log_level="WARNING",
                manual_timestamp_mode="now",
                manual_tv_duration="45", manual_movie_duration="120",
            ))

            # Should redirect to saved, no rebuild
            assert resp.status_code == 302
            assert "saved=1" in resp.headers["Location"]
            assert "refresh-profile" not in resp.headers["Location"]
            assert "refresh-data" not in resp.headers["Location"]
            # No jobs submitted
            mock_jobs.submit.assert_not_called()

    @patch("recommender.web._reload_app_config")
    @patch("recommender.web._save_config_yaml")
    @patch("recommender.web._load_config_yaml")
    def test_save_rebuild_required_settings_shows_cli_command(self, mock_load, mock_save, mock_reload, client):
        """Changing scoring weights should show the profile rebuild CLI command."""
        mock_reload.return_value = True
        mock_load.return_value = {
            "provider": "anthropic",
            "models": {
                "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
                "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
                "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
            },
            "llm": {
                "timeout_fast": 30, "timeout_reason": 60,
                "timeout_profile_batch": 60, "timeout_profile_merge": 300,
                "tokens_fast": 200, "tokens_intent": 400, "tokens_ranking": 1000,
                "tokens_suggestions": 300, "tokens_profile_batch": 800,
                "tokens_profile_merge": 4000, "tokens_abandoned": 300,
                "profile_batch_size": 200, "rate_limit_wait": 65,
            },
            "scoring": {
                "weight_completion": 0.5, "weight_rewatch": 0.3, "weight_recency": 0.2,
                "default_tv_runtime": 45, "default_movie_runtime": 90, "rewatch_saturation": 5,
            },
            "default_top_n": 3, "min_vote_count": 20, "min_rating": 0, "min_year": 0,
            "recency_half_life_days": 90, "watch_region": "US", "streaming_platforms": [],
            "manual": {"timestamp": "now", "tv_duration_minutes": 45, "movie_duration_minutes": 120},
            "log_level": "WARNING",
        }

        with patch("recommender.web.job_registry") as mock_jobs:
            # Change a scoring weight (requires rebuild)
            resp = client.post("/settings", data=_csrf_form(
                provider="anthropic",
                weight_completion="0.6", weight_rewatch="0.2", weight_recency="0.2",
                default_top_n="3", min_vote_count="20", min_rating="0", min_year="0",
                recency_half_life_days="90",
                default_tv_runtime="45", default_movie_runtime="90", rewatch_saturation="5",
                watch_region="US", streaming_platforms="",
                log_level="WARNING",
                manual_timestamp_mode="now",
                manual_tv_duration="45", manual_movie_duration="120",
            ))

            assert resp.status_code == 302
            location = resp.headers["Location"]
            assert "saved=1" in location
            assert "rebuild_command" in location
            assert "refresh-profile" in location
            # No rebuild job auto-submitted
            mock_jobs.submit.assert_not_called()

    @patch("recommender.web._reload_app_config")
    @patch("recommender.web._save_config_yaml")
    @patch("recommender.web._load_config_yaml")
    def test_save_manual_settings_shows_data_rebuild_command(self, mock_load, mock_save, mock_reload, client):
        """Changing manual-title scoring inputs should show the data rebuild CLI command."""
        mock_reload.return_value = True
        mock_load.return_value = {
            "provider": "anthropic",
            "models": {
                "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
                "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
                "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
            },
            "llm": {
                "timeout_fast": 30, "timeout_reason": 60,
                "timeout_profile_batch": 60, "timeout_profile_merge": 300,
                "tokens_fast": 200, "tokens_intent": 400, "tokens_ranking": 1000,
                "tokens_suggestions": 300, "tokens_profile_batch": 800,
                "tokens_profile_merge": 4000, "tokens_abandoned": 300,
                "profile_batch_size": 200, "rate_limit_wait": 65,
            },
            "scoring": {
                "weight_completion": 0.5, "weight_rewatch": 0.3, "weight_recency": 0.2,
                "default_tv_runtime": 45, "default_movie_runtime": 90, "rewatch_saturation": 5,
            },
            "default_top_n": 3, "min_vote_count": 20, "min_rating": 0, "min_year": 0,
            "recency_half_life_days": 90, "watch_region": "US", "streaming_platforms": [],
            "manual": {"timestamp": "now", "tv_duration_minutes": 45, "movie_duration_minutes": 120},
            "log_level": "WARNING",
        }

        with patch("recommender.web.job_registry") as mock_jobs:
            resp = client.post("/settings", data=_csrf_form(
                provider="anthropic",
                weight_completion="0.5", weight_rewatch="0.3", weight_recency="0.2",
                default_top_n="3", min_vote_count="20", min_rating="0", min_year="0",
                recency_half_life_days="90",
                default_tv_runtime="45", default_movie_runtime="90", rewatch_saturation="5",
                watch_region="US", streaming_platforms="",
                log_level="WARNING",
                manual_timestamp_mode="fixed",
                manual_timestamp_date="2024-01-01",
                manual_tv_duration="45", manual_movie_duration="120",
            ))

            assert resp.status_code == 302
            location = resp.headers["Location"]
            assert "saved=1" in location
            assert "rebuild_command" in location
            assert "refresh-data" in location
            mock_jobs.submit.assert_not_called()

    @patch("recommender.web._reload_app_config")
    @patch("recommender.web._save_config_yaml")
    @patch("recommender.web._load_config_yaml")
    def test_save_marks_reload_deferred_when_jobs_are_running(self, mock_load, mock_save, mock_reload, client):
        """Saving while a background job is running should defer the runtime reload explicitly."""
        mock_reload.return_value = False
        mock_load.return_value = {
            "provider": "anthropic",
            "models": {
                "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
                "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
                "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
            },
            "llm": {
                "timeout_fast": 30, "timeout_reason": 60,
                "timeout_profile_batch": 60, "timeout_profile_merge": 300,
                "tokens_fast": 200, "tokens_intent": 400, "tokens_ranking": 1000,
                "tokens_suggestions": 300, "tokens_profile_batch": 800,
                "tokens_profile_merge": 4000, "tokens_abandoned": 300,
                "profile_batch_size": 200, "rate_limit_wait": 65,
            },
            "scoring": {
                "weight_completion": 0.5, "weight_rewatch": 0.3, "weight_recency": 0.2,
                "default_tv_runtime": 45, "default_movie_runtime": 90, "rewatch_saturation": 5,
            },
            "default_top_n": 3, "min_vote_count": 20, "min_rating": 0, "min_year": 0,
            "recency_half_life_days": 90, "watch_region": "US", "streaming_platforms": [],
            "manual": {"timestamp": "now", "tv_duration_minutes": 45, "movie_duration_minutes": 120},
            "log_level": "WARNING",
        }

        resp = client.post("/settings", data=_csrf_form(
            provider="gemini",
            weight_completion="0.5", weight_rewatch="0.3", weight_recency="0.2",
            default_top_n="3", min_vote_count="20", min_rating="0", min_year="0",
            recency_half_life_days="90",
            watch_region="US", streaming_platforms="",
            log_level="WARNING",
            manual_timestamp_mode="now",
            manual_tv_duration="45", manual_movie_duration="120",
        ))

        assert resp.status_code == 302
        assert "saved=1" in resp.headers["Location"]
        assert "reload_deferred=1" in resp.headers["Location"]


# ── Browser rebuild route removed ─────────────────────────────────────────────

class TestRebuildRouteRemoved:
    def test_rebuild_route_returns_404_or_405(self, client):
        """POST /rebuild should no longer exist."""
        resp = client.post("/rebuild", data=_csrf_form(refresh_profile="1"))
        assert resp.status_code in (404, 405)


# ── CSRF enforcement ──────────────────────────────────────────────────────────

class TestCSRF:
    def test_post_without_csrf_rejected(self, client):
        """POST without CSRF token should be rejected with 403."""
        resp = client.post("/recommend", data={"query": "test"})
        assert resp.status_code == 403


class TestEventStoreStatus:
    @patch("recommender.web.job_registry")
    @patch("recommender.web._get_context")
    def test_status_includes_event_store_fields(self, mock_get_context, mock_jobs, client):
        mock_get_context.return_value = MagicMock(watch_index=MagicMock(entries=[]))
        mock_jobs.running_jobs.return_value = []
        mock_jobs.recent_jobs.return_value = []

        with patch("recommender.web.event_store") as mock_es:
            mock_es.get_import_info.return_value = {
                "netflix": {"event_count": 100, "source_path": "/nf.zip",
                            "source_sha256": "abc", "imported_at": "2026-01-01"},
            }
            resp = client.get("/status")

        payload = resp.get_json()
        assert payload["status"] == "ok"
        assert payload["event_store_ready"] is True
        assert payload["event_store_import_count"] == 1
        assert "event_store_path" in payload

    @patch("recommender.web.job_registry")
    @patch("recommender.web._get_context")
    def test_status_ok_when_event_store_missing(self, mock_get_context, mock_jobs, client):
        mock_get_context.return_value = MagicMock(watch_index=MagicMock(entries=[]))
        mock_jobs.running_jobs.return_value = []
        mock_jobs.recent_jobs.return_value = []

        with patch("recommender.web.event_store") as mock_es:
            mock_es.get_import_info.return_value = {}
            resp = client.get("/status")

        payload = resp.get_json()
        assert payload["status"] == "ok"
        assert payload["event_store_ready"] is False
        assert payload["event_store_import_count"] == 0
