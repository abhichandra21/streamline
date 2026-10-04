"""Tests for POST /plex/webhook."""
import base64
import json
from pathlib import Path

import pytest

import config
from recommender import plex, web
from recommender.event_store import init_db, load_events
from recommender.web import app

FIXTURES = Path(__file__).parent / "fixtures" / "plex"
TOKEN = "webhook-secret"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "streamline.db")
    init_db(path)
    monkeypatch.setattr(config, "EVENT_DB_PATH", path)
    monkeypatch.setattr(config, "FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr(config, "PLEX_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setattr(config, "PLEX_URL", "")
    monkeypatch.setattr(config, "PLEX_TOKEN", "")
    monkeypatch.delenv("STREAMLINE_PASSWORD", raising=False)
    return path


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c


def _post(client, fixture="scrobble_movie.json", token=TOKEN, payload=None, **kwargs):
    body = payload if payload is not None else (FIXTURES / fixture).read_text()
    query = f"?token={token}" if token is not None else ""
    return client.post(f"/plex/webhook{query}", data={"payload": body},
                       content_type="multipart/form-data", **kwargs)


def test_a_scrobble_is_saved_as_a_plex_play(client, db):
    response = _post(client)

    assert response.status_code == 200
    assert response.get_json()["status"] == "saved"
    (event,) = load_events(db, provider="plex")
    assert (event.title, event.tmdb_id_hint) == ("House of Gucci", 644495)


def test_the_same_webhook_twice_leaves_one_play(client, db):
    _post(client)
    response = _post(client)

    assert response.get_json()["status"] == "duplicate"
    assert len(load_events(db, provider="plex")) == 1


@pytest.mark.parametrize("token", [None, "wrong"])
def test_a_missing_or_wrong_token_is_refused_and_writes_nothing(client, db, token):
    response = _post(client, token=token)

    assert response.status_code == 403
    assert load_events(db) == []


def test_every_request_is_refused_when_no_webhook_token_is_configured(client, db, monkeypatch):
    monkeypatch.setattr(config, "PLEX_WEBHOOK_TOKEN", "")

    assert _post(client, token="").status_code == 403
    assert load_events(db) == []


@pytest.mark.parametrize("fixture", ["play_movie.json", "stop_episode.json"])
def test_other_playback_events_are_acknowledged_and_ignored(client, db, fixture):
    response = _post(client, fixture=fixture)

    assert response.status_code == 200
    assert response.get_json()["status"] == "ignored"
    assert load_events(db) == []


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]"])
def test_a_missing_or_malformed_payload_is_a_bad_request(client, db, payload):
    response = _post(client, payload=payload)

    assert response.status_code == 400
    assert load_events(db) == []


def test_a_scrobble_without_a_play_time_is_a_bad_request(client, db):
    payload = json.loads((FIXTURES / "scrobble_movie.json").read_text())
    del payload["Metadata"]["lastViewedAt"]

    assert _post(client, payload=json.dumps(payload)).status_code == 400
    assert load_events(db) == []


def test_a_database_failure_is_reported_as_a_server_error(client, db, monkeypatch):
    import sqlite3

    def broken(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(plex.event_store, "append_provider_events", broken)

    assert _post(client).status_code == 500


def test_the_webhook_needs_no_password_or_csrf_but_other_posts_still_do(client, db, monkeypatch):
    monkeypatch.setenv("STREAMLINE_PASSWORD", "household")

    assert _post(client).status_code == 200

    assert client.post("/archive/add", data={"title": "Up"}).status_code == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"user:household").decode()}
    assert client.post("/archive/add", data={"title": "Up"}, headers=auth).status_code == 403


def test_the_client_is_reused_so_show_lookups_stay_cached(client, db, monkeypatch):
    monkeypatch.setattr(config, "PLEX_URL", "http://plex.local:32400")
    monkeypatch.setattr(config, "PLEX_TOKEN", "plex-token")
    monkeypatch.setattr(web, "_plex_client", None)

    assert web._get_plex_client() is web._get_plex_client()
    assert isinstance(web._get_plex_client(), plex.PlexClient)


def test_no_client_without_plex_url_and_token(client, db, monkeypatch):
    monkeypatch.setattr(web, "_plex_client", None)

    assert web._get_plex_client() is None
