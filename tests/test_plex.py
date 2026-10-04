"""Tests for recommender.plex: webhook payloads, the Plex client, and the rating sync."""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from recommender import plex, user_store
from recommender.event_store import init_db, load_events

FIXTURES = Path(__file__).parent / "fixtures" / "plex"


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeClient:
    """Stands in for PlexClient: no network."""

    def __init__(self, show_ids=None, rated=None, fail_show=False, fail_ratings=False):
        self.show_ids = show_ids or {}
        self.rated = rated or []
        self.fail_show = fail_show
        self.fail_ratings = fail_ratings
        self.show_lookups: list[str] = []

    def show_tmdb_id(self, rating_key):
        self.show_lookups.append(rating_key)
        if self.fail_show:
            raise plex.PlexError("show lookup failed")
        return self.show_ids.get(rating_key)

    def rated_items(self):
        if self.fail_ratings:
            raise plex.PlexError("ratings failed")
        return list(self.rated)


def _rated(title, rating, rated_at, kind="movie", tmdb_id=None):
    item = {"type": kind, "title": title, "userRating": rating, "lastRatedAt": rated_at}
    if tmdb_id:
        item["Guid"] = [{"id": f"tmdb://{tmdb_id}"}]
    return item


@pytest.fixture
def db(tmp_path, monkeypatch):
    import config

    path = str(tmp_path / "streamline.db")
    init_db(path)
    user_store.init_db(path)
    monkeypatch.setattr(config, "FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    return path


# ---------------------------------------------------------------------------
# Payload to watch event
# ---------------------------------------------------------------------------

def test_movie_scrobble_becomes_a_plex_watch_event():
    event = plex.event_from_payload(_payload("scrobble_movie.json"), FakeClient())

    assert event.platform == "plex"
    assert event.content_type == "movie"
    assert event.title == event.series_name == "House of Gucci"
    assert event.tmdb_id_hint == 644495
    assert event.release_year_hint == 2021
    assert event.profile == "household-member"
    assert event.timestamp == datetime(2026, 10, 4, 6, 20, 3)  # lastViewedAt, naive UTC
    assert event.watched_duration == event.total_duration == timedelta(milliseconds=9466784)


def test_episode_scrobble_takes_the_show_id_from_plex():
    client = FakeClient(show_ids={"4132": 1831})

    event = plex.event_from_payload(_payload("scrobble_episode.json"), client)

    assert event.content_type == "tv"
    assert event.series_name == "Grand Designs"
    assert event.title == "Grand Designs: Season 25: Newhaven Revisit 2024 (Episode 6)"
    assert event.tmdb_id_hint == 1831
    assert event.release_year_hint is None
    assert client.show_lookups == ["4132"]


def test_episode_is_kept_without_an_id_when_the_show_lookup_fails():
    event = plex.event_from_payload(_payload("scrobble_episode.json"), FakeClient(fail_show=True))

    assert event.series_name == "Grand Designs"
    assert event.tmdb_id_hint is None


def test_episode_is_kept_without_an_id_when_there_is_no_client():
    event = plex.event_from_payload(_payload("scrobble_episode.json"), None)

    assert event.tmdb_id_hint is None


@pytest.mark.parametrize("fixture", ["play_movie.json", "stop_episode.json"])
def test_events_other_than_scrobble_are_ignored(fixture):
    assert plex.event_from_payload(_payload(fixture), FakeClient()) is None


def test_non_video_scrobbles_are_ignored():
    payload = _payload("scrobble_movie.json")
    payload["Metadata"]["type"] = "track"

    assert plex.event_from_payload(payload, FakeClient()) is None


# ---------------------------------------------------------------------------
# handle_webhook
# ---------------------------------------------------------------------------

def test_handle_webhook_saves_a_scrobble_once(db):
    payload = _payload("scrobble_movie.json")

    first = plex.handle_webhook(payload, db, FakeClient())
    second = plex.handle_webhook(payload, db, FakeClient())

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    events = load_events(db, provider="plex")
    assert [(e.title, e.tmdb_id_hint) for e in events] == [("House of Gucci", 644495)]


def test_handle_webhook_ignores_other_events(db):
    result = plex.handle_webhook(_payload("play_movie.json"), db, FakeClient())

    assert result["status"] == "ignored"
    assert load_events(db) == []


def test_handle_webhook_syncs_ratings_after_a_save(db):
    client = FakeClient(rated=[_rated("The Big Short", 8.0, time.time() + 60, tmdb_id=318846)])

    result = plex.handle_webhook(_payload("scrobble_movie.json"), db, client)

    assert result["rating_changes"] == 1
    assert user_store.load_ratings(db)[0]["rating"] == user_store.RATING_MORE


def test_handle_webhook_keeps_the_play_when_the_rating_sync_fails(db):
    result = plex.handle_webhook(_payload("scrobble_movie.json"), db, FakeClient(fail_ratings=True))

    assert result["status"] == "saved"
    assert result["rating_changes"] == 0
    assert len(load_events(db, provider="plex")) == 1


# ---------------------------------------------------------------------------
# Rating sync
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, band", [
    (10.0, "more"), (8.0, "more"), (7.0, "neutral"), (6.0, "neutral"),
    (5.0, "less"), (1.0, "less"), (0.0, None), (None, None),
])
def test_rating_bands(value, band):
    assert plex.rating_band(value) == band


def test_sync_adds_a_rating_streamline_does_not_have(db):
    client = FakeClient(rated=[_rated("House of Gucci", 6.0, 1791094053, tmdb_id=644495)])

    changes = plex.sync_ratings(db, client)

    assert [(c.title, c.old, c.new) for c in changes] == [("House of Gucci", None, "neutral")]
    (rating,) = user_store.load_ratings(db)
    assert (rating["content_type"], rating["tmdb_id"]) == ("movie", 644495)


def test_sync_replaces_an_older_streamline_rating(db):
    user_store.rate_title(db, "House of Gucci", "movie", "less", tmdb_id=644495)
    client = FakeClient(rated=[_rated("House of Gucci", 8.0, time.time() + 60, tmdb_id=644495)])

    changes = plex.sync_ratings(db, client)

    assert [(c.old, c.new) for c in changes] == [("less", "more")]
    assert user_store.load_ratings(db)[0]["rating"] == "more"


def test_sync_keeps_a_newer_streamline_rating(db):
    user_store.rate_title(db, "House of Gucci", "movie", "less", tmdb_id=644495)
    client = FakeClient(rated=[_rated("House of Gucci", 8.0, 1738738601, tmdb_id=644495)])

    assert plex.sync_ratings(db, client) == []
    assert user_store.load_ratings(db)[0]["rating"] == "less"


def test_sync_matches_a_streamline_rating_by_title_when_it_has_no_tmdb_id(db):
    user_store.rate_title(db, "House of Gucci", "movie", "less")
    client = FakeClient(rated=[_rated("House of Gucci", 8.0, time.time() + 60, tmdb_id=644495)])

    changes = plex.sync_ratings(db, client)

    assert [(c.old, c.new) for c in changes] == [("less", "more")]
    assert len(user_store.load_ratings(db)) == 1


def test_sync_rates_shows_as_tv(db):
    client = FakeClient(rated=[_rated("Grand Designs", 9.0, 1791094053, kind="show", tmdb_id=1831)])

    plex.sync_ratings(db, client)

    (rating,) = user_store.load_ratings(db)
    assert (rating["content_type"], rating["rating"]) == ("tv", "more")


def test_sync_ignores_season_and_episode_ratings(db):
    client = FakeClient(rated=[
        _rated("Season 25", 8.0, 1791094053, kind="season"),
        _rated("Newhaven Revisit 2024", 8.0, 1791094053, kind="episode"),
    ])

    assert plex.sync_ratings(db, client) == []
    assert user_store.load_ratings(db) == []


def test_a_rating_cleared_in_streamline_stays_cleared(db):
    client = FakeClient(rated=[_rated("House of Gucci", 6.0, 1791094053, tmdb_id=644495)])
    plex.sync_ratings(db, client)
    user_store.rate_title(db, "House of Gucci", "movie", "clear", tmdb_id=644495)

    assert plex.sync_ratings(db, client) == []
    assert user_store.load_ratings(db) == []


def test_removing_a_rating_in_plex_changes_nothing(db):
    plex.sync_ratings(db, FakeClient(rated=[_rated("House of Gucci", 6.0, 1791094053, tmdb_id=644495)]))

    assert plex.sync_ratings(db, FakeClient(rated=[])) == []
    assert user_store.load_ratings(db)[0]["rating"] == "neutral"


def test_the_mark_is_the_scan_start_so_a_rating_set_during_a_scan_is_read_again(db):
    scan_start = 1791094000
    during_scan = scan_start + 5
    client = FakeClient(rated=[_rated("House of Gucci", 6.0, during_scan, tmdb_id=644495)])

    plex.sync_ratings(db, client, now=lambda: scan_start)
    assert user_store.get_meta(db, plex.RATINGS_MARK_KEY) == str(scan_start)

    # Read again on the next run, and not applied twice.
    assert plex.sync_ratings(db, client, now=lambda: scan_start + 60) == []
    assert user_store.load_ratings(db)[0]["rating"] == "neutral"


def test_a_failed_sync_leaves_the_mark_alone(db):
    user_store.set_meta(db, plex.RATINGS_MARK_KEY, "1000")

    with pytest.raises(plex.PlexError):
        plex.sync_ratings(db, FakeClient(fail_ratings=True))

    assert user_store.get_meta(db, plex.RATINGS_MARK_KEY) == "1000"


def test_ratings_without_a_rated_time_are_treated_as_older(db):
    user_store.rate_title(db, "House of Gucci", "movie", "less", tmdb_id=644495)
    item = _rated("House of Gucci", 8.0, None, tmdb_id=644495)

    assert plex.sync_ratings(db, FakeClient(rated=[item])) == []
    assert user_store.load_ratings(db)[0]["rating"] == "less"


# ---------------------------------------------------------------------------
# PlexClient
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.split("32400", 1)[1]
        self.calls.append((path, params, headers))
        assert timeout == plex.REQUEST_TIMEOUT_SECONDS
        return FakeResponse(self.routes[path])


def _client(routes):
    session = FakeSession(routes)
    return plex.PlexClient("http://plex.local:32400/", "secret-token", session=session), session


def test_client_looks_each_show_up_once():
    client, session = _client({"/library/metadata/4132": {"MediaContainer": {"Metadata": [
        {"Guid": [{"id": "imdb://tt0421099"}, {"id": "tmdb://1831"}]}]}}})

    assert client.show_tmdb_id("4132") == 1831
    assert client.show_tmdb_id("4132") == 1831
    assert len(session.calls) == 1
    assert session.calls[0][2]["X-Plex-Token"] == "secret-token"


def test_client_lists_rated_movies_and_shows_with_their_ids():
    client, session = _client({
        "/library/sections": {"MediaContainer": {"Directory": [
            {"key": "3", "type": "movie"}, {"key": "2", "type": "show"}, {"key": "9", "type": "artist"}]}},
        "/library/sections/3/all": {"MediaContainer": {"Metadata": [_rated("Up", 8.0, 1)]}},
        "/library/sections/2/all": {"MediaContainer": {}},
    })

    assert [i["title"] for i in client.rated_items()] == ["Up"]
    section_calls = [c for c in session.calls if c[0].endswith("/all")]
    assert [c[0] for c in section_calls] == ["/library/sections/3/all", "/library/sections/2/all"]
    assert section_calls[0][1] == {"userRating>>": "0.1", "includeGuids": "1"}


def test_client_turns_network_failures_into_plex_errors():
    import requests

    class BrokenSession:
        def get(self, *_a, **_kw):
            raise requests.ConnectionError("no route to host")

    client = plex.PlexClient("http://plex.local:32400", "t", session=BrokenSession())
    with pytest.raises(plex.PlexError):
        client.rated_items()


def test_client_from_config_needs_both_url_and_token(monkeypatch):
    import config

    monkeypatch.setattr(config, "PLEX_URL", "http://plex.local:32400")
    monkeypatch.setattr(config, "PLEX_TOKEN", "")
    assert plex.client_from_config() is None

    monkeypatch.setattr(config, "PLEX_TOKEN", "t")
    assert isinstance(plex.client_from_config(), plex.PlexClient)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_ratings_prints_what_it_changed(db, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "EVENT_DB_PATH", db)
    client = FakeClient(rated=[_rated("House of Gucci", 6.0, 1791094053, tmdb_id=644495)])
    monkeypatch.setattr(plex, "client_from_config", lambda: client)

    assert plex.main(["ratings"]) == 0

    out = capsys.readouterr().out
    assert "House of Gucci" in out and "neutral" in out


def test_cli_ratings_fails_clearly_without_configuration(db, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "EVENT_DB_PATH", db)
    monkeypatch.setattr(plex, "client_from_config", lambda: None)

    assert plex.main(["ratings"]) == 1
    assert "PLEX_URL" in capsys.readouterr().err
