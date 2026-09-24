"""Tests for the read-only /api/ endpoints used by the Home Assistant dashboard."""

import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recommender import api as api_module
from recommender import web
from recommender.web import app

_REAL_SHOW_PAGE_DATA = web._show_page_data


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c


def _sections(coming_soon=None):
    return {
        "ready_now": [{
            "tmdb_id": 10,
            "title": "Ready Show",
            "season_number": 3,
            "available_episode_count": 2,
            "latest_aired_episode": 4,
            "next_air_date": None,
            "next_episode_number": None,
            "poster_path": "/ready.jpg",
        }],
        "coming_soon": coming_soon if coming_soon is not None else [
            {
                "tmdb_id": 30,
                "title": "Later Show",
                "season_number": 2,
                "next_air_date": "2026-11-01",
                "next_episode_number": 5,
                "poster_path": None,
            },
            {
                "tmdb_id": 20,
                "title": "Premiere, Show; Two",
                "season_number": 4,
                "next_air_date": "2026-10-01",
                "next_episode_number": None,
                "poster_path": "/soon.jpg",
            },
            {
                "tmdb_id": 40,
                "title": "Undated Show",
                "season_number": 1,
                "next_air_date": None,
                "next_episode_number": None,
                "poster_path": None,
            },
        ],
        "might_be_back": [],
        "finished": [],
        "caught_up": [],
        "ignored": [],
    }


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Setup has run, On Deck is cached, and no refresh is due."""
    index = tmp_path / "watch_index.json"
    profile = tmp_path / "taste_profile.txt"
    index.write_text("{}")
    profile.write_text("profile")
    monkeypatch.setattr(web.config, "WATCH_INDEX_PATH", str(index))
    monkeypatch.setattr(web.config, "TASTE_PROFILE_PATH", str(profile))
    monkeypatch.setattr(web.config, "TMDB_API_KEY", "tmdb-key")
    monkeypatch.delenv("STREAMLINE_PASSWORD", raising=False)
    monkeypatch.delenv("STREAMLINE_API_TOKEN", raising=False)

    archive = [{"tmdb_id": 10, "title": "Ready Show", "content_type": "tv"}]
    monkeypatch.setattr(web, "_show_page_data", lambda: (archive, [], _sections()))
    monkeypatch.setattr(web.show_tracker, "refresh_is_due", lambda *_args: False)
    monkeypatch.setattr(web, "_shows_job_id", None)
    monkeypatch.setattr(
        api_module.show_tracker, "last_refresh_at",
        lambda _dir: datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(web, "_get_poster_url", lambda tmdb_id, _ct, size="w300": f"https://img/{tmdb_id}.jpg")
    monkeypatch.setattr(web, "_ensure_user_store_once", lambda: None)
    monkeypatch.setattr(api_module.user_store, "list_saved_titles", lambda _db, status=None: [{
        "title": "Saved Film", "normalized_title": "saved film", "content_type": "movie",
        "tmdb_id": 99, "status": "watchlist", "saved_at": "2026-09-01T10:00:00", "updated_at": None,
    }])
    entries = [
        {"title": "A", "content_type": "tv"},
        {"title": "B", "content_type": "movie"},
        {"title": "C", "content_type": "movie"},
    ]
    monkeypatch.setattr(web, "_get_context", lambda: SimpleNamespace(watch_index=SimpleNamespace(entries=entries)))

    def no_llm(*_args, **_kwargs):
        raise AssertionError("the API must never build an LLM client")

    monkeypatch.setattr(web, "create_client", no_llm)
    return archive


class TestShapes:
    def test_summary(self, client, ready):
        body = client.get("/api/summary").get_json()

        assert body["ready_now_count"] == 1
        assert body["coming_soon_count"] == 3
        assert body["watchlist_count"] == 1
        assert body["next_up"]["tmdb_id"] == 20
        assert body["next_up"]["next_air_date"] == "2026-10-01"
        assert body["next_up"]["poster_url"] == "https://image.tmdb.org/t/p/w300/soon.jpg"
        assert body["library"] == {"total": 3, "tv": 1, "movies": 2}
        assert body["shows_checked_at"] == "2026-09-24T06:00:00+00:00"
        assert body["shows_refreshing"] is False
        assert body["taste_profile_built_at"]

    def test_on_deck_sorts_soonest_first_with_undated_last(self, client, ready):
        body = client.get("/api/on-deck").get_json()

        assert [c["tmdb_id"] for c in body["coming_soon"]] == [20, 30, 40]
        ready_card = body["ready_now"][0]
        assert ready_card["latest_aired_episode"] == 4
        assert ready_card["available_episode_count"] == 2
        assert ready_card["poster_url"] == "https://image.tmdb.org/t/p/w300/ready.jpg"
        # A card without a snapshot poster falls back to the TMDB title cache.
        assert body["coming_soon"][1]["poster_url"] == "https://img/30.jpg"
        # Every card carries every key, so templates can rely on the shape.
        keys = set(ready_card)
        assert all(set(card) == keys for card in body["coming_soon"])

    def test_watchlist(self, client, ready):
        body = client.get("/api/watchlist").get_json()

        assert body == {"watchlist": [{
            "title": "Saved Film",
            "content_type": "movie",
            "tmdb_id": 99,
            "saved_at": "2026-09-01T10:00:00",
            "poster_url": "https://img/99.jpg",
        }]}

    def test_empty_sections_are_lists_and_next_up_is_null(self, client, ready, monkeypatch):
        empty = {key: [] for key in _sections()}
        monkeypatch.setattr(web, "_show_page_data", lambda: ([], [], empty))
        monkeypatch.setattr(api_module.user_store, "list_saved_titles", lambda _db, status=None: [])

        on_deck = client.get("/api/on-deck").get_json()
        summary = client.get("/api/summary").get_json()

        assert on_deck["ready_now"] == []
        assert on_deck["coming_soon"] == []
        assert summary["next_up"] is None
        assert summary["ready_now_count"] == 0
        assert client.get("/api/watchlist").get_json() == {"watchlist": []}


class _NoLLM:
    def __getattr__(self, name):
        raise AssertionError(f"the API touched the LLM client ({name})")


class TestNoLLM:
    @pytest.mark.parametrize("path", [
        "/api/summary", "/api/on-deck", "/api/watchlist", "/api/coming-soon.ics",
    ])
    def test_real_show_data_path_never_uses_the_llm(self, client, ready, monkeypatch, path):
        # Undo the fixture's stub so the request builds On Deck the real way.
        monkeypatch.setattr(web, "_show_page_data", _REAL_SHOW_PAGE_DATA)
        archive = [{"tmdb_id": 10, "title": "Ready Show", "content_type": "tv"}]
        ctx = SimpleNamespace(watch_index=SimpleNamespace(entries=archive), llm=_NoLLM())
        monkeypatch.setattr(web, "_get_context", lambda: ctx)
        monkeypatch.setattr(web.user_store, "list_manual_archive", lambda _db: [])
        monkeypatch.setattr(web.user_store, "list_show_tracking", lambda _db: [])
        monkeypatch.setattr(web.show_tracker, "load_snapshots", lambda _dir: {})

        response = client.get(path)

        assert response.status_code == 200


class TestNotReady:
    @pytest.mark.parametrize("path", [
        "/api/summary", "/api/on-deck", "/api/watchlist", "/api/coming-soon.ics",
    ])
    def test_503_before_setup(self, client, tmp_path, monkeypatch, path):
        monkeypatch.delenv("STREAMLINE_PASSWORD", raising=False)
        monkeypatch.setattr(web.config, "WATCH_INDEX_PATH", str(tmp_path / "missing.json"))
        monkeypatch.setattr(web.config, "TASTE_PROFILE_PATH", str(tmp_path / "missing.txt"))

        response = client.get(path)

        assert response.status_code == 503
        assert response.get_json()["status"] == "not ready"


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"ha:{password}".encode()).decode()}


class TestAuth:
    def test_open_when_no_password(self, client, ready):
        assert client.get("/api/summary").status_code == 200

    def test_password_required_when_set(self, client, ready, monkeypatch):
        monkeypatch.setenv("STREAMLINE_PASSWORD", "pw")

        assert client.get("/api/summary").status_code == 401
        assert client.get("/api/summary", headers=_basic("wrong")).status_code == 401
        assert client.get("/api/summary", headers=_basic("pw")).status_code == 200

    def test_token_accepted_for_api_reads(self, client, ready, monkeypatch):
        monkeypatch.setenv("STREAMLINE_PASSWORD", "pw")
        monkeypatch.setenv("STREAMLINE_API_TOKEN", "tok")

        ok = client.get("/api/summary", headers={"Authorization": "Bearer tok"})
        wrong = client.get("/api/summary", headers={"Authorization": "Bearer nope"})

        assert ok.status_code == 200
        assert wrong.status_code == 401

    def test_token_rejected_outside_api(self, client, ready, monkeypatch):
        monkeypatch.setenv("STREAMLINE_PASSWORD", "pw")
        monkeypatch.setenv("STREAMLINE_API_TOKEN", "tok")

        response = client.get("/status", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 401

    def test_token_rejected_for_api_writes(self, client, ready, monkeypatch):
        monkeypatch.setenv("STREAMLINE_PASSWORD", "pw")
        monkeypatch.setenv("STREAMLINE_API_TOKEN", "tok")

        response = client.post("/api/summary", headers={"Authorization": "Bearer tok"})

        assert response.status_code == 401

    def test_token_unset_accepts_no_bearer(self, client, ready, monkeypatch):
        monkeypatch.setenv("STREAMLINE_PASSWORD", "pw")

        response = client.get("/api/summary", headers={"Authorization": "Bearer "})

        assert response.status_code == 401


class TestIcal:
    def test_feed_is_valid_all_day_events(self, client, ready):
        response = client.get("/api/coming-soon.ics")
        body = response.data.decode()

        assert response.status_code == 200
        assert response.content_type.startswith("text/calendar")
        assert body.startswith("BEGIN:VCALENDAR\r\n")
        assert body.endswith("END:VCALENDAR\r\n")
        assert "\n" not in body.replace("\r\n", "")
        assert body.count("BEGIN:VEVENT") == 2  # the undated show is skipped
        assert "DTSTART;VALUE=DATE:20261001\r\n" in body
        assert "DTEND;VALUE=DATE:20261002\r\n" in body
        assert "SUMMARY:Premiere\\, Show\\; Two S4 premiere\r\n" in body
        assert "SUMMARY:Later Show S2E5\r\n" in body
        for line in body.split("\r\n"):
            assert len(line.encode()) <= 75

    def test_uids_are_stable_across_requests(self, client, ready):
        def uids():
            body = client.get("/api/coming-soon.ics").data.decode()
            return [line for line in body.split("\r\n") if line.startswith("UID:")]

        first = uids()

        assert first == uids()
        assert len(set(first)) == 2
        assert "UID:streamline-30-s2-e5" in first

    def test_lone_carriage_return_is_escaped(self, client, ready, monkeypatch):
        sections = _sections([{
            "tmdb_id": 60, "title": "Bad\rTitle", "season_number": 1,
            "next_air_date": "2026-10-05", "next_episode_number": 2, "poster_path": None,
        }])
        monkeypatch.setattr(web, "_show_page_data", lambda: ([], [], sections))

        body = client.get("/api/coming-soon.ics").data.decode()

        assert "\r" not in body.replace("\r\n", "")
        assert "SUMMARY:Bad\\nTitle S1E2\r\n" in body

    def test_missing_season_and_episode_zero_stay_distinct(self, client, ready, monkeypatch):
        sections = _sections([
            {"tmdb_id": 70, "title": "Odd Show", "season_number": None,
             "next_air_date": "2026-10-05", "next_episode_number": None, "poster_path": None},
            {"tmdb_id": 70, "title": "Odd Show", "season_number": 1,
             "next_air_date": "2026-10-06", "next_episode_number": 0, "poster_path": None},
            {"tmdb_id": 70, "title": "Odd Show", "season_number": 1,
             "next_air_date": "2026-10-07", "next_episode_number": None, "poster_path": None},
        ])
        monkeypatch.setattr(web, "_show_page_data", lambda: ([], [], sections))

        body = client.get("/api/coming-soon.ics").data.decode()
        uids = [line for line in body.split("\r\n") if line.startswith("UID:")]

        assert "None" not in body
        assert "SUMMARY:Odd Show premiere\r\n" in body
        assert len(set(uids)) == 3

    def test_long_lines_are_folded(self, client, ready, monkeypatch):
        long_title = "A" * 120
        sections = _sections([{
            "tmdb_id": 50, "title": long_title, "season_number": 1,
            "next_air_date": "2026-10-05", "next_episode_number": 1, "poster_path": None,
        }])
        monkeypatch.setattr(web, "_show_page_data", lambda: ([], [], sections))

        body = client.get("/api/coming-soon.ics").data.decode()
        unfolded = body.replace("\r\n ", "")

        assert f"SUMMARY:{long_title} S1E1" in unfolded


class TestRefreshParity:
    """The API refreshes On Deck exactly as GET /shows does."""

    @pytest.mark.parametrize("path", ["/api/summary", "/api/on-deck", "/api/coming-soon.ics"])
    def test_starts_a_due_refresh(self, client, ready, monkeypatch, path):
        monkeypatch.setattr(web.show_tracker, "refresh_is_due", lambda *_args: True)
        registry = MagicMock()
        registry.submit.return_value = "shows-job"
        monkeypatch.setattr(web, "job_registry", registry)

        response = client.get(path)

        assert response.status_code == 200
        registry.submit.assert_called_once()
        assert registry.submit.call_args.args[0] is web._run_show_refresh
        assert registry.submit.call_args.args[1] is ready

    @pytest.mark.parametrize("path", ["/api/summary", "/api/on-deck", "/api/coming-soon.ics"])
    def test_does_not_start_when_not_due(self, client, ready, monkeypatch, path):
        registry = MagicMock()
        monkeypatch.setattr(web, "job_registry", registry)

        client.get(path)

        registry.submit.assert_not_called()

    def test_reuses_the_running_job_shared_with_shows(self, client, ready, monkeypatch):
        monkeypatch.setattr(web.show_tracker, "refresh_is_due", lambda *_args: True)
        registry = MagicMock()
        registry.submit.return_value = "shows-job"
        registry.get.return_value = MagicMock(status="running", progress=None)
        monkeypatch.setattr(web, "job_registry", registry)

        client.get("/shows")
        body = client.get("/api/summary").get_json()
        client.get("/api/on-deck")

        registry.submit.assert_called_once()
        assert body["shows_refreshing"] is True

    def test_watchlist_does_not_refresh(self, client, ready, monkeypatch):
        monkeypatch.setattr(web.show_tracker, "refresh_is_due", lambda *_args: True)
        registry = MagicMock()
        monkeypatch.setattr(web, "job_registry", registry)

        client.get("/api/watchlist")

        registry.submit.assert_not_called()
