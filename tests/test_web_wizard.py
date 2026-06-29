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
    # Landing describes the feature and waits for an explicit Start (no auto-fire).
    assert b"Mood Match" in resp.data
    assert b"Start" in resp.data
    assert b'hx-trigger="load"' not in resp.data


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


def test_question_can_render_single_select_mode(client):
    fake_turn = {"action": "ask", "prompt": "Movie or series?", "subtext": "",
                 "chips": [{"label": "Movie", "value": "movie"},
                           {"label": "Series", "value": "tv"}],
                 "multi": False, "allow_free_text": False}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0})),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b'type="radio"' in resp.data


def test_question_renders_checkbox_for_multi_select(client):
    fake_turn = {"action": "ask", "prompt": "Which moods?", "subtext": "",
                 "chips": [{"label": "Light", "value": "light"},
                           {"label": "Tense", "value": "tense"}],
                 "multi": True, "allow_free_text": False}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0})),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b'type="checkbox"' in resp.data


def _full_intent_dict(**overrides):
    base = {
        "genres": [], "origin_countries": [], "languages": [], "mood_descriptors": [],
        "similar_to": [], "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None, "content_type": "both",
        "top_n": 5, "platforms": [],
    }
    base.update(overrides)
    return base


def test_wizard_job_records_structured_history():
    intent_dict = _full_intent_dict(max_runtime_minutes=90, content_type="movie")
    captured = {}

    def fake_record(query, results, provider, usage_summary, *, metadata=None):
        captured["query"] = query
        captured["metadata"] = metadata

    with patch.object(web, "_get_job_context") as gjc, \
         patch.object(web, "ask", return_value=[]), \
         patch.object(web, "_build_result_items", return_value=[]), \
         patch.object(web.query_history, "record", side_effect=fake_record):
        gjc.return_value.llm.provider = "fake"
        gjc.return_value.llm.usage.summary.return_value = "usage"
        web._run_wizard_recommend_job(intent_dict, "low energy", "something short and light")

    md = captured["metadata"]
    assert md["source"] == "wizard"
    assert md["label"].startswith("Mood Match")
    assert md["summary"] == "something short and light"
    assert md["intent_dict"] == intent_dict
    assert md["context_note"] == "low energy"


def test_searches_render_label_when_present(client):
    entries = [{
        "timestamp": "2026-06-29T12:00:00+00:00",
        "query": "mood match: something short and light",
        "label": "Mood Match - short and light",
        "source": "wizard",
        "summary": "something short and light",
        "provider": "fake", "results": [], "usage": "",
    }]
    with patch.object(web.query_history, "load", return_value=entries):
        resp = client.get("/searches")
    assert resp.status_code == 200
    assert b"Mood Match - short and light" in resp.data


def test_searches_fall_back_to_query_for_old_entries(client):
    entries = [{
        "timestamp": "2026-06-29T12:00:00+00:00",
        "query": "good british crime drama",
        "provider": "fake", "results": [], "usage": "",
    }]
    with patch.object(web.query_history, "load", return_value=entries):
        resp = client.get("/searches")
    assert resp.status_code == 200
    assert b"good british crime drama" in resp.data


def test_wizard_history_replay_uses_stored_intent(client):
    intent_dict = _full_intent_dict(max_runtime_minutes=90, content_type="movie")
    with patch.object(web.job_registry, "submit", return_value="job-replay") as sub:
        resp = client.post("/wizard/replay", data=_csrf_form(
            intent=json.dumps(intent_dict),
            context_note="low energy",
            summary="something short and light",
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"job-replay" in resp.data
    sub.assert_called_once()
    args = sub.call_args.args
    assert args[0] is web._run_wizard_recommend_job
    assert args[1] == intent_dict
    assert args[2] == "low energy"
    assert args[3] == "something short and light"


def test_wizard_history_replay_rejects_missing_payload(client):
    with patch.object(web.job_registry, "submit") as sub:
        resp = client.post("/wizard/replay", data=_csrf_form(
            context_note="low energy", summary="x",
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 400
    sub.assert_not_called()


def test_classic_history_research_still_uses_query(client):
    entries = [{
        "timestamp": "2026-06-29T12:00:00+00:00",
        "query": "good british crime drama",
        "provider": "fake", "results": [], "usage": "",
    }]
    with patch.object(web.query_history, "load", return_value=entries):
        resp = client.get("/searches")
    assert resp.status_code == 200
    assert b"/?q=" in resp.data
    assert b"/wizard/replay" not in resp.data


def test_searches_wizard_entry_renders_replay_form(client):
    entries = [{
        "timestamp": "2026-06-29T12:00:00+00:00",
        "query": "mood match: something short and light",
        "label": "Mood Match - short and light",
        "source": "wizard",
        "summary": "something short and light",
        "intent_dict": _full_intent_dict(content_type="movie"),
        "context_note": "low energy",
        "provider": "fake", "results": [], "usage": "",
    }]
    with patch.object(web.query_history, "load", return_value=entries):
        resp = client.get("/searches")
    assert resp.status_code == 200
    assert b"/wizard/replay" in resp.data


def test_wizard_refine_shorter_updates_runtime():
    # No runtime yet: "shorter" sets one based on content type.
    intent_dict = _full_intent_dict(content_type="movie", max_runtime_minutes=None)
    new_intent, _ = web._apply_refinement(intent_dict, "low energy", "shorter")
    assert new_intent["max_runtime_minutes"] is not None
    assert new_intent["max_runtime_minutes"] <= 120

    # Existing runtime: "shorter" reduces it with a sensible floor.
    intent2 = _full_intent_dict(content_type="movie", max_runtime_minutes=120)
    reduced, _ = web._apply_refinement(intent2, "", "shorter")
    assert reduced["max_runtime_minutes"] < 120


def test_wizard_refine_more_obscure_adjusts_context():
    intent_dict = _full_intent_dict()
    _, note = web._apply_refinement(intent_dict, "low energy", "more obscure")
    assert "obscure" in note.lower() or "lesser" in note.lower()


def test_wizard_refine_rejects_bad_intent_json(client):
    with patch.object(web.job_registry, "submit") as sub:
        resp = client.post("/wizard/refine", data=_csrf_form(
            intent="{not valid", directive="shorter", summary="x"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 400
    sub.assert_not_called()


def test_wizard_refine_excludes_shown_titles_as_json(client):
    intent_json = json.dumps(_full_intent_dict())
    shown = json.dumps([
        {"title": "A Movie", "content_type": "movie", "tmdb_id": 1},
        {"title": "B Show", "content_type": "tv", "tmdb_id": 2},
    ])
    with patch.object(web.job_registry, "submit", return_value="job-r") as sub:
        resp = client.post("/wizard/refine", data=_csrf_form(
            intent=intent_json, directive="surprise me", shown=shown, summary="x"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    exclude = sub.call_args.args[4]
    assert "A Movie" in exclude
    assert "B Show" in exclude


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
