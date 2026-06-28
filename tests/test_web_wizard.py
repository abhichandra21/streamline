"""Tests for the concierge wizard routes."""

import json
from unittest.mock import patch

import pytest

from recommender import web
from recommender.query_engine import QueryIntent
from recommender.web import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf-token"
        yield c


def _csrf_form(**kwargs):
    return {"_csrf_token": "test-csrf-token", **kwargs}


def test_get_wizard_renders_shell(client):
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert b"wizard-stage" in resp.data


def test_post_next_ask_branch_renders_question(client):
    fake_turn = {"action": "ask", "prompt": "Vibe tonight?", "subtext": "",
                 "chips": [{"label": "Light", "value": "light"}],
                 "multi": True, "allow_free_text": True}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0}),
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"Vibe tonight?" in resp.data
    assert b"light" in resp.data


def test_post_next_recommend_branch_starts_job(client):
    intent = QueryIntent(
        genres=[], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="both",
        top_n=5, platforms=[])
    fake_turn = {"action": "recommend", "summary": "light and short",
                 "intent": intent, "context_note": "low energy"}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"), \
         patch.object(web.job_registry, "submit", return_value="job-123") as sub:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 1}),
            prompt="Vibe tonight?", selected=["light"], free_text="",
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"job-123" in resp.data   # polling partial carries the job id
    sub.assert_called_once()


def test_question_renders_progress_segments(client):
    from unittest.mock import patch
    import json
    fake_turn = {"action": "ask", "prompt": "Q?", "subtext": "",
                 "chips": [{"label": "A", "value": "a"}], "multi": False,
                 "allow_free_text": False}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0})),
            headers={"HX-Request": "true"})
    assert b"wiz-progress" in resp.data


def test_post_refine_starts_job(client):
    intent_json = json.dumps({
        "genres": [], "origin_countries": [], "languages": [], "mood_descriptors": [],
        "similar_to": [], "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None, "content_type": "both",
        "top_n": 5, "platforms": [],
    })
    with patch.object(web.job_registry, "submit", return_value="job-xyz") as sub:
        resp = client.post("/wizard/refine", data=_csrf_form(
            intent=intent_json,
            context_note="low energy",
            directive="lighter",
            shown="A, B",
            summary="light picks",
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"job-xyz" in resp.data
    sub.assert_called_once()
