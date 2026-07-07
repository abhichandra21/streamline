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

        monkeypatch.setattr(web.config, "PLATFORM_PATHS", {"netflix": ["/tmp/export.zip"], "prime": [], "apple_tv": []})
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


class TestTitleDetailFallback:
    @patch("recommender.web._load_enrichments", return_value={})
    @patch("recommender.web._load_user_state")
    @patch("recommender.web._get_context")
    def test_title_detail_rejects_unrelated_alternate_type_cache(
        self, mock_ctx, mock_user_state, _mock_enrichments, client
    ):
        from recommender.tmdb_client import TmdbMetadata

        user_state = MagicMock()
        user_state.is_manually_watched.return_value = False
        user_state.is_in_watchlist.return_value = False
        user_state.is_dismissed.return_value = False
        user_state.get_rating.return_value = None
        mock_user_state.return_value = user_state

        alt_meta = TmdbMetadata(
            tmdb_id=55063,
            content_type="tv",
            title="Man on the Moon: The Epic Journey of Apollo 11",
        )
        tmdb_client = MagicMock()
        tmdb_client.get_cached_by_id.side_effect = lambda tmdb_id, ct: alt_meta if ct == "tv" else None

        mock_ctx.return_value = MagicMock(
            tmdb_client=tmdb_client,
            watch_index=MagicMock(
                tmdb_ids={55063},
                tmdb_keys={("movie", 55063)},
                entries=[{"tmdb_id": 55063, "title": "Apollo 11", "content_type": "movie"}],
            ),
        )

        resp = client.get("/title/55063?type=movie")

        html = resp.data.decode()
        assert resp.status_code == 200
        assert "Title not found in cache" in html
        assert "Man on the Moon" not in html

    @patch("recommender.web._load_enrichments", return_value={})
    @patch("recommender.web._load_user_state")
    @patch("recommender.web._get_context")
    def test_title_detail_accepts_episode_title_alternate_type_cache(
        self, mock_ctx, mock_user_state, _mock_enrichments, client
    ):
        from recommender.tmdb_client import TmdbMetadata

        user_state = MagicMock()
        user_state.is_manually_watched.return_value = False
        user_state.is_in_watchlist.return_value = False
        user_state.is_dismissed.return_value = False
        user_state.get_rating.return_value = None
        mock_user_state.return_value = user_state

        alt_meta = TmdbMetadata(tmdb_id=66732, content_type="tv", title="Stranger Things")
        tmdb_client = MagicMock()
        tmdb_client.get_cached_by_id.side_effect = lambda tmdb_id, ct: alt_meta if ct == "tv" else None

        mock_ctx.return_value = MagicMock(
            tmdb_client=tmdb_client,
            watch_index=MagicMock(
                tmdb_ids={66732},
                tmdb_keys={("movie", 66732)},
                entries=[{
                    "tmdb_id": 66732,
                    "title": "Stranger Things: Stranger Things 4: Chapter One: The Hellfire Club (Episode 1)",
                    "content_type": "movie",
                }],
            ),
        )

        resp = client.get("/title/66732?type=movie")

        html = resp.data.decode()
        assert resp.status_code == 200
        assert "Stranger Things" in html
        assert "Title not found in cache" not in html

    @patch("recommender.web._load_enrichments", return_value={})
    @patch("recommender.web._load_user_state")
    @patch("recommender.web._get_context")
    def test_title_detail_accepts_exact_short_title_alternate_type_cache(
        self, mock_ctx, mock_user_state, _mock_enrichments, client
    ):
        from recommender.tmdb_client import TmdbMetadata

        user_state = MagicMock()
        user_state.is_manually_watched.return_value = False
        user_state.is_in_watchlist.return_value = False
        user_state.is_dismissed.return_value = False
        user_state.get_rating.return_value = None
        mock_user_state.return_value = user_state

        alt_meta = TmdbMetadata(tmdb_id=11, content_type="tv", title="Up")
        tmdb_client = MagicMock()
        tmdb_client.get_cached_by_id.side_effect = lambda tmdb_id, ct: alt_meta if ct == "tv" else None

        mock_ctx.return_value = MagicMock(
            tmdb_client=tmdb_client,
            watch_index=MagicMock(
                tmdb_ids={11},
                tmdb_keys={("movie", 11)},
                entries=[{"tmdb_id": 11, "title": "Up", "content_type": "movie"}],
            ),
        )

        resp = client.get("/title/11?type=movie")

        html = resp.data.decode()
        assert resp.status_code == 200
        assert "Up" in html
        assert "Title not found in cache" not in html


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


class TestWatchlistRoutes:

    def test_save_to_watchlist(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, list_saved_titles

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/watchlist/save", data={
            **_csrf_form(),
            "title": "Breaking Bad",
            "content_type": "tv",
        })
        assert resp.status_code == 200
        items = list_saved_titles(db, status="watchlist")
        assert len(items) == 1

    def test_dismiss_title(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title, list_saved_titles

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "Bad Show", "tv")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/watchlist/dismiss", data={
            **_csrf_form(),
            "title": "Bad Show",
            "content_type": "tv",
        })
        assert resp.status_code == 200
        assert len(list_saved_titles(db, status="dismissed")) == 1

    def test_remove_from_watchlist(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title, list_saved_titles

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "Some Show", "tv")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/watchlist/remove", data={
            **_csrf_form(),
            "title": "Some Show",
            "content_type": "tv",
        })
        assert resp.status_code == 200
        assert list_saved_titles(db) == []

    def test_mark_watched_returns_rating_prompt(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "The Wire", "tv")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/watchlist/watched", data={
            **_csrf_form(),
            "title": "The Wire",
            "content_type": "tv",
        })
        assert resp.status_code == 200
        assert len(list_manual_archive(db)) == 1
        # Response should contain rating prompt
        assert b"liked" in resp.data or b"thumbs" in resp.data.lower()


class TestArchiveRoutes:

    def test_add_to_archive(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/archive/add", data={
            **_csrf_form(),
            "title": "The Bear",
            "content_type": "tv",
        })
        assert resp.status_code == 200
        assert len(list_manual_archive(db)) == 1

    def test_rate_archive_title(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, load_ratings

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/archive/rate", data={
            **_csrf_form(),
            "title": "Breaking Bad",
            "content_type": "tv",
            "rating": "liked",
        })
        assert resp.status_code == 200
        ratings = load_ratings(db)
        assert len(ratings) == 1
        assert ratings[0]["rating"] == "liked"

    def test_clear_rating(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, rate_title, load_ratings

        db = str(tmp_path / "test.db")
        init_db(db)
        rate_title(db, "Breaking Bad", "tv", "liked")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.post("/archive/rate", data={
            **_csrf_form(),
            "title": "Breaking Bad",
            "content_type": "tv",
            "rating": "clear",
        })
        assert resp.status_code == 200
        assert load_ratings(db) == []


class TestRenderedState:

    def test_watchlist_badge_count(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "Show A", "tv")
        save_title(db, "Show B", "tv")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.get("/watchlist")
        assert resp.status_code == 200
        # Badge count and items|length both render "2" on the page
        assert b"2" in resp.data

    def test_watchlist_page_renders_items(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "Breaking Bad", "tv")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        resp = client.get("/watchlist")
        assert b"Breaking Bad" in resp.data
        assert b"Mark as watched" in resp.data  # action button present
        assert b"Won&#39;t watch" in resp.data or b"Won't watch" in resp.data

    def test_watchlist_page_renders_cached_metadata(self, client, tmp_path, monkeypatch):
        """A saved title with cached TMDB metadata shows its poster, rating, genres,
        and streaming availability rather than a bare text row."""
        from recommender import web
        from recommender.user_store import init_db, save_title
        from recommender.tmdb_client import TmdbMetadata
        from unittest.mock import MagicMock

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "Calibre", "movie", tmdb_id=474051)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))
        monkeypatch.setattr("config.TMDB_API_KEY", "tmdb")

        meta = TmdbMetadata(tmdb_id=474051, content_type="movie", title="Calibre",
                            genres=["Thriller", "Drama"], vote_average=6.9)
        tmdb_client = MagicMock()
        tmdb_client.get_cached_by_id.return_value = meta
        tmdb_client._load_cache.return_value = {"imdb_id": "tt6230738"}
        tmdb_client.get_watch_providers.return_value = ["Netflix"]
        monkeypatch.setattr(web, "_get_context", lambda: MagicMock(tmdb_client=tmdb_client))
        monkeypatch.setattr(web, "_get_poster_url",
                            lambda tid, ct, size="w300": "https://image.tmdb.org/t/p/w300/x.jpg")

        resp = client.get("/watchlist")
        html = resp.data.decode()
        assert resp.status_code == 200
        assert "image.tmdb.org" in html              # poster rendered
        assert "6.9" in html                          # rating shown
        assert "Thriller" in html                     # genres shown
        assert "Netflix" in html                      # streaming availability shown
        assert "tt6230738" in html                    # direct IMDB link from cached id

    def test_archive_rate_shows_thumbs_state(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, rate_title

        db = str(tmp_path / "test.db")
        init_db(db)
        rate_title(db, "Breaking Bad", "tv", "liked")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        # Rate endpoint should return the _rating_state partial
        resp = client.post("/archive/rate", data={
            **_csrf_form(),
            "title": "Breaking Bad",
            "content_type": "tv",
            "rating": "liked",
        })
        assert resp.status_code == 200
        # Should show the liked state (clear button for liked thumb)
        assert b"clear" in resp.data

    def test_history_dedup_does_not_double_show_manual_entry(self, client, tmp_path, monkeypatch):
        """A manual archive entry already in the watch index should not appear twice."""
        from recommender.user_store import init_db, add_to_archive
        from unittest.mock import MagicMock, patch

        db = str(tmp_path / "test.db")
        init_db(db)
        add_to_archive(db, "Duplicate Show", "tv", source="web")
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

        # Simulate watch index already having the same title
        mock_ctx = MagicMock()
        mock_ctx.watch_index.entries = [
            {"title": "Duplicate Show", "content_type": "tv", "tmdb_id": None}
        ]

        with patch("recommender.web._get_context", return_value=mock_ctx):
            resp = client.get("/history")

        assert resp.status_code == 200
        # hist-1 is the uid for the first item; hist-2 would only appear if there are two items
        assert b"Duplicate Show" in resp.data
        assert b"hist-2" not in resp.data  # only one item in the deduped list


def test_history_includes_manual_archive_entries(client, tmp_path, monkeypatch):
    from recommender.user_store import init_db, add_to_archive
    from unittest.mock import MagicMock, patch

    db = str(tmp_path / "test.db")
    init_db(db)
    add_to_archive(db, "Manual Show", "tv", source="web")
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = []

    with patch("recommender.web._get_context", return_value=mock_ctx):
        resp = client.get("/history")

    assert resp.status_code == 200
    assert b"Manual Show" in resp.data


def test_history_provider_filter_and_recent_sort(client, tmp_path, monkeypatch):
    """Issue #37: filter the archive by source provider and sort by recency."""
    from unittest.mock import MagicMock, patch
    from recommender.user_store import init_db

    db = str(tmp_path / "test.db")
    init_db(db)
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = [
        {"title": "Netflix Pick", "content_type": "movie", "tmdb_id": None,
         "platforms": ["netflix"], "last_watched": "2026-01-01T00:00:00"},
        {"title": "Prime Pick", "content_type": "movie", "tmdb_id": None,
         "platforms": ["prime"], "last_watched": "2026-05-01T00:00:00"},
    ]
    with patch("recommender.web._get_context", return_value=mock_ctx):
        # Dropdown offers a source filter.
        resp = client.get("/history")
        assert b"All sources" in resp.data

        # Filtering by provider keeps only that source.
        resp = client.get("/history?platform=netflix")
        assert b"Netflix Pick" in resp.data
        assert b"Prime Pick" not in resp.data

        # Recent sort puts the most recently watched first.
        html = client.get("/history?sort=recent").data.decode()
        assert html.index("Prime Pick") < html.index("Netflix Pick")


def test_history_merges_manual_provenance_into_existing_entry(client, tmp_path, monkeypatch):
    """Issue #37 review: a manual watch of an already-indexed title folds its
    'manual' source into the existing row instead of being dropped."""
    from unittest.mock import MagicMock, patch
    from recommender.user_store import init_db, add_to_archive

    db = str(tmp_path / "test.db")
    init_db(db)
    add_to_archive(db, "Imported Show", "tv", source="web")
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = [
        {"title": "Imported Show", "content_type": "tv", "tmdb_id": None,
         "platforms": ["netflix"], "last_watched": "2026-01-01T00:00:00"},
    ]
    with patch("recommender.web._get_context", return_value=mock_ctx):
        # The manual source filter now finds the already-indexed title.
        assert b"Imported Show" in client.get("/history?platform=manual").data
        # The original source is still present (no in-place mutation of the cache).
        resp = client.get("/history?platform=netflix")
        assert b"Imported Show" in resp.data
        # Still a single row, not a duplicate.
        assert resp.data.count(b"hist-2") == 0


def test_history_keeps_distinct_manual_entries_with_different_tmdb_ids(client, tmp_path, monkeypatch):
    """Review fix: two manual rows sharing a title/type but with different TMDB
    ids (e.g. remakes) must stay distinct, not collapse via a title-only key."""
    from unittest.mock import MagicMock, patch
    from recommender.user_store import init_db, add_to_archive

    db = str(tmp_path / "test.db")
    init_db(db)
    add_to_archive(db, "The Office", "tv", tmdb_id=2316)   # US
    add_to_archive(db, "The Office", "tv", tmdb_id=2996)   # UK remake
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = []
    with patch("recommender.web._get_context", return_value=mock_ctx):
        resp = client.get("/history?platform=manual&per_page=120")
        # Two separate list rows (hist-2 only appears with a second item)...
        assert b"hist-2" in resp.data
        # ...and not a spurious third.
        assert b"hist-3" not in resp.data


def test_consolidate_providers_groups_variants():
    """Issue #36: granular TMDB provider names collapse to one entry per brand."""
    from recommender.web import _consolidate_providers

    out = _consolidate_providers([
        "Netflix", "Netflix Standard with Ads",
        "Amazon Prime Video", "Amazon Prime Video with Ads", "Freevee Amazon Channel",
        "Paramount+ Amazon Channel", "Apple TV+",
    ])
    assert out == ["Netflix", "Amazon Prime Video", "Freevee", "Paramount+", "Apple TV+"]


def test_history_rating_filter(client, tmp_path, monkeypatch):
    """Issue #36: filter the archive by rating status."""
    from unittest.mock import MagicMock, patch
    from recommender.user_store import init_db, rate_title

    db = str(tmp_path / "test.db")
    init_db(db)
    rate_title(db, "Liked Movie", "movie", "liked")
    monkeypatch.setattr("config.EVENT_DB_PATH", db)
    monkeypatch.setattr("config.FEEDBACK_PATH", str(tmp_path / "feedback.json"))

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = [
        {"title": "Liked Movie", "content_type": "movie", "tmdb_id": None,
         "platforms": [], "last_watched": ""},
        {"title": "Unrated Movie", "content_type": "movie", "tmdb_id": None,
         "platforms": [], "last_watched": ""},
    ]
    with patch("recommender.web._get_context", return_value=mock_ctx):
        resp = client.get("/history?rating=liked")
        assert b"Liked Movie" in resp.data
        assert b"Unrated Movie" not in resp.data

        resp = client.get("/history?rating=unrated")
        assert b"Unrated Movie" in resp.data
        assert b"Liked Movie" not in resp.data


def test_history_pagination_url_encodes_query(client):
    from unittest.mock import MagicMock, patch

    mock_ctx = MagicMock()
    mock_ctx.watch_index.entries = [
        {"title": f"Law & Order Case {i:02d}", "content_type": "tv", "tmdb_id": None}
        for i in range(61)
    ]

    with patch("recommender.web._get_context", return_value=mock_ctx), \
         patch("recommender.web._load_user_state") as mock_user_state, \
         patch("recommender.web.user_store.list_manual_archive", return_value=[]):
        user_state = MagicMock()
        user_state.get_rating.return_value = None
        mock_user_state.return_value = user_state
        resp = client.get("/history?q=law+%26+order&per_page=30")

    assert resp.status_code == 200
    assert b"page=2" in resp.data
    assert b"q=law+%26+order" in resp.data


class TestArchiveAdd:
    """Manual archive add: TMDB resolution and stale-flag behavior."""

    def _post(self, client, title: str, content_type: str = "tv"):
        return client.post("/archive/add", data={
            **_csrf_form(),
            "title": title,
            "content_type": content_type,
        })

    def test_strong_match_stores_tmdb_id(self, client, tmp_path, monkeypatch):
        """When TMDB resolves with high confidence, tmdb_id is persisted."""
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            MockClient.return_value.resolve_title_confident.return_value = (12345, "tv")
            resp = self._post(client, "Next Gen Chef")

        assert resp.status_code == 200
        entries = list_manual_archive(db)
        assert len(entries) == 1
        assert entries[0]["tmdb_id"] == 12345

    def test_weak_match_succeeds_without_bad_tmdb_id(self, client, tmp_path, monkeypatch):
        """When TMDB returns no confident match, the entry is saved without a tmdb_id."""
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            MockClient.return_value.resolve_title_confident.return_value = (None, "tv")
            resp = self._post(client, "Ambiguous Title")

        assert resp.status_code == 200
        entries = list_manual_archive(db)
        assert len(entries) == 1
        assert entries[0]["tmdb_id"] is None

    def test_tmdb_error_still_saves_entry(self, client, tmp_path, monkeypatch):
        """A TMDB API failure is swallowed; the entry is saved without a tmdb_id."""
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            MockClient.return_value.resolve_title_confident.side_effect = RuntimeError("network error")
            resp = self._post(client, "Some Show")

        assert resp.status_code == 200
        entries = list_manual_archive(db)
        assert len(entries) == 1
        assert entries[0]["tmdb_id"] is None

    def test_stale_flag_is_set_after_add(self, client, tmp_path, monkeypatch):
        """Profile stale flag is touched after any manual archive add."""
        from recommender.user_store import init_db

        db = str(tmp_path / "test.db")
        init_db(db)
        stale_flag = tmp_path / ".profile_stale"
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(stale_flag))

        resp = self._post(client, "Any Show")

        assert resp.status_code == 200
        assert stale_flag.exists()

    def test_strong_match_fetches_and_caches_detail_json(self, client, tmp_path, monkeypatch):
        """After a strong TMDB match, detail JSON is fetched and cached for immediate overview/poster use."""
        from recommender.user_store import init_db

        db = str(tmp_path / "test.db")
        init_db(db)
        cache_dir = tmp_path / "tmdb"
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")
        monkeypatch.setattr("config.CACHE_DIR", str(cache_dir))
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        detail_data = {"overview": "A cooking competition.", "poster_path": "/abc.jpg"}

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            instance = MockClient.return_value
            instance.resolve_title_confident.return_value = (12345, "tv")
            instance._load_cache.return_value = None
            instance._fetch_details.return_value = detail_data
            self._post(client, "Next Gen Chef")
            instance._fetch_details.assert_called_once_with(12345, "tv")
            instance._save_cache.assert_called_once_with("tv", 12345, detail_data)

    def test_no_llm_called_from_archive_add(self, client, tmp_path, monkeypatch):
        """No LLM enrichment is triggered from the archive add request path."""
        from recommender.user_store import init_db

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        with patch("recommender.llm.create_client") as mock_llm:
            self._post(client, "Some Show")

        mock_llm.assert_not_called()


class TestArchiveResolve:
    def _post(self, client, title="The Bear", content_type="tv"):
        return client.post("/archive/resolve", data={
            **_csrf_form(),
            "title": title,
            "content_type": content_type,
        })

    def test_missing_title_returns_400(self, client):
        resp = self._post(client, title="")
        assert resp.status_code == 400

    def test_no_api_key_renders_unmatched_only_state(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")

        resp = self._post(client)
        assert resp.status_code == 200
        assert b"no API key configured" in resp.data

    def test_renders_candidates_with_watchlist_conflict(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title
        from recommender.tmdb_client import DisambiguationCandidate, DisambiguationResult

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "The Bear", "tv", tmdb_id=194583)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            MockClient.return_value.get_disambiguation_candidates.return_value = DisambiguationResult(
                candidates=[DisambiguationCandidate(
                    tmdb_id=194583, content_type="tv", title="The Bear",
                    year=2022, poster_path=None, score=90.0,
                )],
            )
            resp = self._post(client)

        assert resp.status_code == 200
        assert b"Already on your watchlist" in resp.data

    def test_both_searches_failing_shows_error_state(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db
        from recommender.tmdb_client import DisambiguationResult

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            MockClient.return_value.get_disambiguation_candidates.return_value = DisambiguationResult(
                candidates=[], hinted_type_failed=True, alternate_type_failed=True,
            )
            resp = self._post(client)

        assert resp.status_code == 200
        assert b"TMDB lookup failed" in resp.data


class TestArchiveConfirm:
    def _post(self, client, **overrides):
        data = {
            **_csrf_form(),
            "title": "The Bear",
            "content_type": "tv",
            "resolution": "add",
            "tmdb_id": "194583",
        }
        data.update(overrides)
        return client.post("/archive/confirm", data=data)

    def test_missing_title_returns_400(self, client):
        resp = self._post(client, title="")
        assert resp.status_code == 400

    def test_add_without_tmdb_id_returns_400(self, client):
        resp = self._post(client, tmdb_id="")
        assert resp.status_code == 400

    def test_invalid_resolution_returns_400(self, client):
        resp = self._post(client, resolution="bogus")
        assert resp.status_code == 400

    def test_unmatched_saves_with_no_tmdb_id(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        resp = self._post(client, resolution="unmatched", tmdb_id="")

        assert resp.status_code == 200
        entries = list_manual_archive(db)
        assert len(entries) == 1
        assert entries[0]["tmdb_id"] is None

    def test_add_fetches_canonical_title_before_saving(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "test-key")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        with patch("recommender.tmdb_client.TmdbClient") as MockClient:
            instance = MockClient.return_value
            instance._load_cache.return_value = None
            instance._fetch_details.return_value = {"name": "The Bear (Canonical)"}
            resp = self._post(client, title="the bear typo")

        assert resp.status_code == 200
        entries = list_manual_archive(db)
        assert len(entries) == 1
        assert entries[0]["title"] == "The Bear (Canonical)"

    def test_mark_watched_moves_from_watchlist_to_archive(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db, save_title, list_saved_titles, list_manual_archive

        db = str(tmp_path / "test.db")
        init_db(db)
        save_title(db, "The Bear", "tv", tmdb_id=194583)
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(tmp_path / ".profile_stale"))

        resp = self._post(client, resolution="mark_watched")

        assert resp.status_code == 200
        assert list_saved_titles(db) == []
        assert len(list_manual_archive(db)) == 1

    def test_stale_flag_touched_after_confirm(self, client, tmp_path, monkeypatch):
        from recommender.user_store import init_db

        db = str(tmp_path / "test.db")
        init_db(db)
        stale_flag = tmp_path / ".profile_stale"
        monkeypatch.setattr("config.EVENT_DB_PATH", db)
        monkeypatch.setattr("config.TMDB_API_KEY", "")
        monkeypatch.setattr("config.PROFILE_STALE_FLAG", str(stale_flag))

        self._post(client, resolution="unmatched", tmdb_id="")

        assert stale_flag.exists()


class TestHistoryTmdbOverview:
    """Archive/history page shows TMDB overview when enrichment text is absent."""

    def test_tmdb_overview_shown_when_no_enrichment(self, client, tmp_path, monkeypatch):
        import json

        tmdb_id = 99001
        content_type = "tv"
        cache_dir = tmp_path / "tmdb" / content_type
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{tmdb_id}.json").write_text(json.dumps({
            "overview": "A cooking competition for the next generation.",
            "poster_path": None,
        }))
        monkeypatch.setattr("config.CACHE_DIR", str(tmp_path / "tmdb"))

        mock_ctx = MagicMock()
        mock_ctx.watch_index.entries = [{
            "title": "Next Gen Chef",
            "content_type": content_type,
            "tmdb_id": tmdb_id,
        }]

        with patch("recommender.web._get_context", return_value=mock_ctx), \
             patch("recommender.web._load_enrichments", return_value={}), \
             patch("recommender.web._load_user_state") as mock_user_state, \
             patch("recommender.web.user_store.list_manual_archive", return_value=[]):
            user_state = MagicMock()
            user_state.get_rating.return_value = None
            mock_user_state.return_value = user_state
            resp = client.get("/history")

        assert resp.status_code == 200
        assert b"next generation" in resp.data.lower()

    def test_get_tmdb_overview_returns_none_when_no_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CACHE_DIR", str(tmp_path / "tmdb"))

        from recommender.web import _get_tmdb_overview
        assert _get_tmdb_overview(99999, "tv") is None

    def test_get_tmdb_overview_returns_none_when_overview_empty(self, tmp_path, monkeypatch):
        import json

        cache_dir = tmp_path / "tmdb" / "tv"
        cache_dir.mkdir(parents=True)
        (cache_dir / "88888.json").write_text(json.dumps({"overview": ""}))
        monkeypatch.setattr("config.CACHE_DIR", str(tmp_path / "tmdb"))

        from recommender.web import _get_tmdb_overview
        assert _get_tmdb_overview(88888, "tv") is None


class TestArchiveDisambiguatePartial:
    def _render(self, **overrides):
        from recommender.web import app
        defaults = dict(
            title="The Bear", content_type="tv",
            api_key_missing=False, both_failed=False,
            hinted_type_failed=False, alternate_type_failed=False,
            candidates=[],
        )
        defaults.update(overrides)
        with app.test_request_context():
            from flask import render_template
            return render_template("_archive_disambiguate.html", **defaults)

    def test_renders_plain_candidate_with_add_button(self):
        html = self._render(candidates=[{
            "tmdb_id": 194583, "content_type": "tv", "title": "The Bear",
            "year": 2022, "poster_path": None, "conflict": None,
        }])
        assert "The Bear" in html
        assert "2022" in html
        assert "value=\"add\"" in html

    def test_renders_watchlist_conflict_with_mark_watched_button(self):
        html = self._render(candidates=[{
            "tmdb_id": 194583, "content_type": "tv", "title": "The Bear",
            "year": 2022, "poster_path": None,
            "conflict": {"source": "watchlist", "title": "The Bear"},
        }])
        assert "Already on your watchlist" in html
        assert "value=\"mark_watched\"" in html

    def test_renders_archive_conflict_with_update_watched_date_label(self):
        html = self._render(candidates=[{
            "tmdb_id": 194583, "content_type": "tv", "title": "The Bear",
            "year": 2022, "poster_path": None,
            "conflict": {"source": "archive", "title": "The Bear"},
        }])
        assert "Already in your archive" in html
        assert "Update watched date" in html

    def test_renders_no_api_key_message(self):
        html = self._render(api_key_missing=True)
        assert "no API key configured" in html

    def test_renders_both_failed_message(self):
        html = self._render(both_failed=True)
        assert "TMDB lookup failed" in html

    def test_renders_no_matches_message_when_search_succeeded_with_zero_results(self):
        html = self._render(candidates=[])
        assert "No matches found" in html

    def test_unmatched_button_always_present(self):
        html = self._render()
        assert "value=\"unmatched\"" in html
        assert "save as unmatched" in html
