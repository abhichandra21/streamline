import json
from recommender import wizard
from recommender.wizard import WizardState
from recommender.query_engine import RecommendContext, QueryIntent


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
    def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
        self.calls.append(prompt)
        return self._responses.pop(0)


def _ctx(llm):
    return RecommendContext(
        taste_profile="Loves slow-burn British crime drama.",
        watch_index=None, tmdb_client=None, llm=llm, cache_dir="", events=[],
    )


def test_next_turn_returns_ask_question():
    llm = FakeLLM([json.dumps({
        "action": "ask",
        "prompt": "What's the vibe tonight?",
        "subtext": "Pick any that fit.",
        "chips": [{"label": "Light", "value": "light"}, {"label": "Tense", "value": "tense"}],
        "multi": True,
        "allow_free_text": True,
    })])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm))
    assert out["action"] == "ask"
    assert out["chips"][0]["value"] == "light"
    assert out["multi"] is True


def test_cap_forces_finalize(monkeypatch):
    monkeypatch.setattr("config.WIZARD_MAX_QUESTIONS", 2)
    recommend_json = json.dumps({
        "action": "recommend", "summary": "light and short",
        "intent": {"content_type": "movie", "top_n": 5},
        "context_note": "30 minutes, low energy",
    })
    llm = FakeLLM([recommend_json])
    state = WizardState(turns=[{}, {}], turn_count=2)  # already at cap
    out = wizard.next_turn(state, _ctx(llm))
    assert out["action"] == "recommend"
    assert isinstance(out["intent"], QueryIntent)
    assert out["intent"].content_type == "movie"
    assert out["context_note"] == "30 minutes, low energy"


def test_malformed_turn_falls_back_to_finalize():
    recommend_json = json.dumps({
        "action": "recommend", "summary": "x",
        "intent": {"content_type": "both"}, "context_note": "",
    })
    llm = FakeLLM(["this is not json", recommend_json])
    out = wizard.next_turn(WizardState(turns=[], turn_count=1), _ctx(llm))
    assert out["action"] == "recommend"
    assert len(llm.calls) == 2   # bad turn, then forced finalize


def test_state_from_json_resets_on_garbage():
    assert wizard.WizardState.from_json("{bad json").turns == []
    s = wizard.WizardState(turns=[{"prompt": "q", "selected": ["a"], "free_text": ""}], turn_count=1)
    assert wizard.WizardState.from_json(s.to_json()).turn_count == 1


def test_finalize_handles_array_response():
    llm = FakeLLM(["[]"])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm), force_finish=True)
    assert out["action"] == "recommend"
    assert isinstance(out["intent"], QueryIntent)


def test_next_turn_array_response_falls_back_to_finalize():
    llm = FakeLLM([
        "[]",
        json.dumps({"action": "recommend", "summary": "x",
                    "intent": {"content_type": "both"}, "context_note": ""}),
    ])
    out = wizard.next_turn(WizardState(turns=[], turn_count=1), _ctx(llm))
    assert out["action"] == "recommend"
    assert len(llm.calls) == 2


def test_wizard_prompt_asks_to_resolve_content_type_early():
    prompt = wizard._prompt(WizardState(turns=[], turn_count=0), "profile", force_finish=False)
    low = prompt.lower()
    assert "content type" in low
    assert "movie" in low and "series" in low and "either" in low
    assert "early" in low
    # The wizard no longer forces every question to be multi-select.
    assert "every question is multi-select" not in low


def test_wizard_prompt_explains_single_select_mode():
    prompt = wizard._prompt(WizardState(turns=[], turn_count=0), "profile", force_finish=False)
    low = prompt.lower()
    assert "single-select" in low
    assert "multi-select" in low


def test_finalize_preserves_max_runtime_minutes():
    recommend_json = json.dumps({
        "action": "recommend", "summary": "short and light",
        "intent": {"content_type": "movie", "max_runtime_minutes": 90, "top_n": 5},
        "context_note": "around 90 minutes",
    })
    llm = FakeLLM([recommend_json])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm), force_finish=True)
    assert out["intent"].max_runtime_minutes == 90


def test_wizard_prompt_maps_time_to_max_runtime():
    prompt = wizard._prompt(WizardState(turns=[], turn_count=0), "profile", force_finish=True)
    low = prompt.lower()
    assert "max_runtime_minutes" in low
    assert "under an hour" in low
    assert "90" in low


def test_wizard_ignores_malformed_chips():
    # Non-list chips payload must not crash next_turn().
    llm = FakeLLM([json.dumps({
        "action": "ask", "prompt": "Vibe?", "subtext": "",
        "chips": "not a list", "multi": True, "allow_free_text": True,
    })])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm))
    assert out["action"] == "ask"
    assert out["chips"] == []


def test_wizard_keeps_only_valid_chips():
    llm = FakeLLM([json.dumps({
        "action": "ask", "prompt": "Vibe?", "subtext": "",
        "chips": [{"label": "Light", "value": "light"}, "junk", {"label": "no value"}],
        "multi": True, "allow_free_text": True,
    })])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm))
    assert out["chips"] == [{"label": "Light", "value": "light"}]


def test_wizard_state_rejects_oversized_payload():
    huge = json.dumps({
        "turns": [{"prompt": "x" * 100} for _ in range(1000)],
        "turn_count": 1000,
    })
    state = wizard.WizardState.from_json(huge)
    assert state.turns == []
    assert state.turn_count == 0
