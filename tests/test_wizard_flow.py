from recommender.wizard import WizardState
from recommender import wizard_flow


def test_first_step_is_content_type():
    turn = wizard_flow.current_turn(WizardState())
    assert turn["action"] == "ask"
    assert turn["step"] == "content_type"
    assert turn["multi"] is False
    assert [c["value"] for c in turn["chips"]] == ["movie", "tv", "both"]


def test_answering_content_type_advances_to_time_window():
    state = WizardState()
    state = wizard_flow.apply_answer(state, "content_type", ["movie"], "")
    assert state.answers["content_type"] == "movie"
    assert state.step == "time_window"


def test_movie_time_choices_do_not_offer_under_an_hour():
    state = WizardState(step="time_window", answers={"content_type": "movie"})
    values = [c["value"] for c in wizard_flow.current_turn(state)["chips"]]
    assert "under_hour" not in values
    assert "short_movie" in values


def test_tv_time_choices_use_episode_language():
    state = WizardState(step="time_window", answers={"content_type": "tv"})
    labels = [c["label"].lower() for c in wizard_flow.current_turn(state)["chips"]]
    assert any("episode" in label for label in labels)


def test_back_moves_to_previous_step_without_appending_answer():
    state = WizardState(
        step="energy",
        answers={"content_type": "movie", "time_window": "short_movie"},
    )
    updated = wizard_flow.previous_step(state)
    assert updated.step == "time_window"
    assert updated.answers["content_type"] == "movie"
    assert updated.answers["time_window"] == "short_movie"


def test_back_from_adaptive_returns_to_review():
    state = WizardState(step="adaptive", answers={"content_type": "movie"})
    updated = wizard_flow.previous_step(state)
    assert updated.step == "review"


def test_editing_content_type_clears_dependent_answers():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny"],
        },
    )
    updated = wizard_flow.edit_step(state, "content_type")
    assert updated.step == "content_type"
    assert updated.answers == {}


def test_build_seed_maps_movie_short_time_to_intent_fields():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny"],
        },
    )
    intent_dict, context_note, summary = wizard_flow.build_recommendation_seed(state)
    assert intent_dict["content_type"] == "movie"
    assert intent_dict["max_runtime_minutes"] == 95
    assert intent_dict["mood_descriptors"] == ["funny"]
    assert "easy" in context_note.lower()
    assert "short" in summary.lower()


def test_build_seed_steers_either_under_hour_to_tv():
    state = WizardState(
        step="review",
        answers={"content_type": "both", "time_window": "under_hour", "energy": "easy"},
    )
    intent_dict, context_note, summary = wizard_flow.build_recommendation_seed(state)
    assert intent_dict["content_type"] == "tv"
    assert intent_dict["max_runtime_minutes"] == 60


def test_review_model_summarizes_structured_answers():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny", "warm"],
        },
    )
    model = wizard_flow.review_model(state)
    assert model["summary"] == "a short, easy movie with a funny, warm tone"
    assert [row["step"] for row in model["answers"]] == [
        "content_type",
        "time_window",
        "energy",
        "tone",
    ]


def test_editing_energy_preserves_earlier_clears_later():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny"],
            "free_text": "nothing bleak",
        },
    )
    updated = wizard_flow.edit_step(state, "energy")
    assert updated.step == "energy"
    assert updated.answers["content_type"] == "movie"
    assert updated.answers["time_window"] == "short_movie"
    assert "energy" not in updated.answers
    assert "tone" not in updated.answers
