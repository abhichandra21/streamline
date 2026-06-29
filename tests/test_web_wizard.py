"""Tests for the concierge wizard routes."""

import json
from html.parser import HTMLParser
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


class _HiddenInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden_inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        data = dict(attrs)
        if data.get("type") == "hidden" and data.get("name"):
            self.hidden_inputs[data["name"]] = data.get("value", "")


def test_get_wizard_renders_shell(client):
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert b"wizard-stage" in resp.data
    # Landing describes the feature and waits for an explicit Start (no auto-fire).
    assert b"Mood Match" in resp.data
    assert b"Start" in resp.data
    assert b'hx-trigger="load"' not in resp.data


def test_wizard_start_renders_deterministic_content_type_without_llm(client):
    with patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(),
                           headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"Movie" in resp.data
    assert b"Series" in resp.data
    next_turn.assert_not_called()


def test_wizard_content_type_advances_to_time_window_without_llm(client):
    with patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0, "step": "content_type"}),
            step="content_type", selected=["movie"],
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # Movie time choices, and still no LLM call.
    assert b"Short" in resp.data
    next_turn.assert_not_called()


def test_post_next_ask_branch_renders_question(client):
    # The adaptive (LLM) question only fires when the user asks for one more.
    fake_turn = {"action": "ask", "prompt": "Vibe tonight?", "subtext": "",
                 "chips": [{"label": "Light", "value": "light"}],
                 "multi": True, "allow_free_text": True}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0, "step": "review"}),
            ask_more="1",
        ), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"Vibe tonight?" in resp.data
    assert b"light" in resp.data


def test_back_action_does_not_append_answer(client):
    state = {"turns": [], "turn_count": 0, "step": "energy",
             "answers": {"content_type": "movie", "time_window": "short_movie"}}
    with patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps(state), step="energy", selected=["easy"], back="1"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    next_turn.assert_not_called()
    parser = _HiddenInputParser()
    parser.feed(resp.data.decode())
    new_state = json.loads(parser.hidden_inputs["state"])
    assert new_state["step"] == "time_window"
    assert new_state["turns"] == []
    # The current question's selection was discarded, not stored.
    assert new_state["answers"] == {"content_type": "movie", "time_window": "short_movie"}


def test_back_renders_saved_answer_as_checked(client):
    import re
    state = {"turns": [], "turn_count": 0, "step": "energy",
             "answers": {"content_type": "movie", "time_window": "short_movie"}}
    with patch.object(web.wizard, "next_turn"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps(state), back="1"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Back lands on time_window with the previously chosen option pre-checked.
    assert re.search(r'value="short_movie"\s+checked', body)


def test_edit_from_review_clears_dependent_answers(client):
    state = {"turns": [], "turn_count": 0, "step": "review",
             "answers": {"content_type": "movie", "time_window": "short_movie",
                         "energy": "easy"}}
    with patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps(state), edit_step="content_type"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    next_turn.assert_not_called()
    assert b"Movie" in resp.data
    parser = _HiddenInputParser()
    parser.feed(resp.data.decode())
    new_state = json.loads(parser.hidden_inputs["state"])
    assert new_state["step"] == "content_type"
    assert new_state["answers"] == {}


def test_review_renders_before_results_without_submitting(client):
    # Answering the last deterministic question (tone) lands on review.
    state = {"turns": [], "turn_count": 0, "step": "tone",
             "answers": {"content_type": "movie", "time_window": "short_movie",
                         "energy": "easy"}}
    with patch.object(web.job_registry, "submit") as sub, \
         patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps(state), step="tone", selected=["funny"]),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data
    assert b"Show picks" in body
    assert b"edit_step" in body
    assert b"a short, easy movie with a funny tone" in body
    sub.assert_not_called()
    next_turn.assert_not_called()


def test_finish_from_review_submits_structured_seed(client):
    state = {"turns": [], "turn_count": 0, "step": "review",
             "answers": {"content_type": "movie", "time_window": "short_movie",
                         "energy": "easy", "tone": ["funny"]}}
    with patch.object(web.job_registry, "submit", return_value="job-seed") as sub, \
         patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps(state), finish="1"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"job-seed" in resp.data
    next_turn.assert_not_called()
    args = sub.call_args.args
    assert args[0] is web._run_wizard_recommend_job
    assert args[1]["content_type"] == "movie"
    assert args[1]["max_runtime_minutes"] == 95
    assert args[1]["mood_descriptors"] == ["funny"]


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
            state=json.dumps({"turns": [], "turn_count": 1, "step": "adaptive"}),
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
    # The deterministic tone step is the multi-select question.
    with patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0, "step": "tone",
                              "answers": {"content_type": "movie"}})),
            headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b'type="checkbox"' in resp.data


def test_wizard_shell_advertises_short_flow(client):
    resp = client.get("/wizard")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Usually 3-4" in body
    assert "stop and see picks at any point" in body


def test_wizard_progress_does_not_use_max_as_expected_count(client):
    import re
    import config as cfg
    fake_turn = {"action": "ask", "prompt": "Q?", "subtext": "",
                 "chips": [{"label": "A", "value": "a"}], "multi": False,
                 "allow_free_text": False}
    with patch.object(cfg, "WIZARD_MAX_QUESTIONS", 8), \
         patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = client.post("/wizard/next", data=_csrf_form(
            state=json.dumps({"turns": [], "turn_count": 0})),
            headers={"HX-Request": "true"})
    body = resp.data.decode()
    m = re.search(r'wiz-progress.*?>(.*?)</div>', body, re.S)
    segments = m.group(1).count("<span") if m else 0
    # The progress visual reflects an expected short flow, not one dot per cap.
    assert 0 < segments < 8


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
    intent_dict = _full_intent_dict(content_type="movie")
    entries = [{
        "timestamp": "2026-06-29T12:00:00+00:00",
        "query": "mood match: something short and light",
        "label": "Mood Match - short and light",
        "source": "wizard",
        "summary": "something short and light",
        "intent_dict": intent_dict,
        "context_note": "low energy",
        "provider": "fake", "results": [], "usage": "",
    }]
    with patch.object(web.query_history, "load", return_value=entries):
        resp = client.get("/searches")
    assert resp.status_code == 200
    assert b"/wizard/replay" in resp.data
    parser = _HiddenInputParser()
    parser.feed(resp.data.decode())
    assert json.loads(parser.hidden_inputs["intent"]) == intent_dict


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


def test_refine_bad_intent_returns_error(client):
    # A JSON value that is not an object must be rejected, not crash a job.
    with patch.object(web.job_registry, "submit") as sub:
        resp = client.post("/wizard/refine", data=_csrf_form(
            intent="[1,2,3]", directive="shorter", summary="x"),
            headers={"HX-Request": "true"})
    assert resp.status_code == 400
    sub.assert_not_called()


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
