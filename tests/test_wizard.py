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


def test_wizard_floor_rejects_early_recommend(monkeypatch):
    monkeypatch.setattr("config.WIZARD_MIN_QUESTIONS", 4)
    monkeypatch.setattr("config.WIZARD_MAX_QUESTIONS", 5)
    recommend = json.dumps({"action": "recommend", "summary": "x",
                            "intent": {"content_type": "both"}, "context_note": ""})
    ask = json.dumps({"action": "ask", "prompt": "What tone?", "subtext": "",
                      "chips": [{"label": "Dark", "value": "dark"}],
                      "multi": False, "allow_free_text": True})
    # Below the floor the model first tries to finish; the retry asks instead.
    llm = FakeLLM([recommend, ask])
    out = wizard.next_turn(WizardState(turns=[], turn_count=1), _ctx(llm))
    assert out["action"] == "ask"
    assert out["prompt"] == "What tone?"
    assert len(llm.calls) == 2


def test_wizard_floor_allows_recommend_once_met(monkeypatch):
    monkeypatch.setattr("config.WIZARD_MIN_QUESTIONS", 4)
    monkeypatch.setattr("config.WIZARD_MAX_QUESTIONS", 5)
    recommend = json.dumps({"action": "recommend", "summary": "x",
                            "intent": {"content_type": "both"}, "context_note": ""})
    llm = FakeLLM([recommend])
    out = wizard.next_turn(WizardState(turns=[{}, {}, {}, {}], turn_count=4), _ctx(llm))
    assert out["action"] == "recommend"
    assert len(llm.calls) == 1   # no retry once the floor is satisfied


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


def test_wizard_prompt_is_engaging_but_plain_and_markdown_free():
    prompt = wizard._prompt(WizardState(turns=[], turn_count=0), "profile", force_finish=False)
    low = prompt.lower()
    # Warm and engaging, grounded in the taste profile, but plain and never markdown.
    assert "taste profile" in low
    assert "engaging" in low
    assert "plain" in low
    assert "no markdown" in low


def test_wizard_prompt_injects_taste_tag_vocabulary():
    tags = "THE USER'S REAL CATEGORIES\nGenres they watch most: Crime, Thriller\n\n"
    prompt = wizard._prompt(WizardState(turns=[], turn_count=0), "profile",
                            force_finish=False, tags=tags)
    assert "Genres they watch most: Crime, Thriller" in prompt


def test_wizard_strips_markdown_from_question_and_chips():
    llm = FakeLLM([json.dumps({
        "action": "ask",
        "prompt": "Closer to *Slow Horses* or _Encanto_ tonight?",
        "subtext": "**Knowing** helps.",
        "chips": [{"label": "*Gripping*", "value": "gripping"}],
        "multi": False, "allow_free_text": True,
    })])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm))
    assert "*" not in out["prompt"] and "_" not in out["prompt"]
    assert "*" not in out["subtext"]
    assert out["chips"][0]["label"] == "Gripping"


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


def test_adaptive_prompt_includes_structured_preferences():
    state = WizardState(
        step="adaptive",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
        },
    )
    prompt = wizard._prompt(state, "profile", force_finish=False)
    low = prompt.lower()
    assert "structured preferences" in low
    assert "content_type: movie" in low
    assert "max_runtime_minutes: 95" in low
    assert "do not ask content type" in low


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


def test_wizard_state_loads_legacy_payload_without_new_fields():
    raw = json.dumps({"turns": [], "turn_count": 0})
    state = wizard.WizardState.from_json(raw)
    assert state.step == "content_type"
    assert state.answers == {}
    assert state.adaptive_turns == []


def test_wizard_state_round_trips_structured_answers():
    state = wizard.WizardState(
        turns=[],
        turn_count=0,
        step="time_window",
        answers={"content_type": "movie"},
        adaptive_turns=[],
    )
    loaded = wizard.WizardState.from_json(state.to_json())
    assert loaded.step == "time_window"
    assert loaded.answers == {"content_type": "movie"}


def test_wizard_state_rejects_oversized_payload():
    huge = json.dumps({
        "turns": [{"prompt": "x" * 100} for _ in range(1000)],
        "turn_count": 1000,
    })
    state = wizard.WizardState.from_json(huge)
    assert state.turns == []
    assert state.turn_count == 0
