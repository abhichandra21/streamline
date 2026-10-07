import json

from recommender.structured_profile import (
    build_structured_profile,
    load_structured_profile,
    parse_structured_profile_response,
    save_structured_profile,
    select_profile_slice,
    validate_structured_profile,
)
from recommender.query_engine import QueryIntent
from tests.mock_llm import make_mock_llm

import pytest

ONE_CLUSTER = json.dumps({"version": 1, "clusters": [{"label": "Any", "members": [1]}]})


@pytest.fixture(autouse=True)
def _structured_path_in_tmp(tmp_path, monkeypatch):
    """Builds save the raw answer next to the structured profile; keep it out of the real cache."""
    import config
    monkeypatch.setattr(config, "STRUCTURED_TASTE_PROFILE_PATH", str(tmp_path / "structured.json"))


def make_intent(**overrides):
    values = {
        "genres": [],
        "origin_countries": [],
        "languages": [],
        "mood_descriptors": [],
        "similar_to": [],
        "max_runtime_minutes": None,
        "year_from": None,
        "year_to": None,
        "unwatched_only": True,
        "special_intent": None,
        "content_type": "both",
        "top_n": 3,
        "platforms": [],
    }
    values.update(overrides)
    return QueryIntent(**values)


def test_parse_structured_profile_response_accepts_fenced_json():
    payload = {
        "version": 1,
        "clusters": [
            {
                "id": "british-crime",
                "label": "British crime",
                "weight": 1.4,
                "positive_traits": ["patient investigations"],
                "negative_traits": ["thin mystery"],
                "co_viewing": "PERSONAL",
                "mood_states": ["serious"],
                "languages": ["en"],
                "regions": ["GB"],
                "representative_titles": ["Broadchurch", "Broadchurch"],
            }
        ],
    }
    result = parse_structured_profile_response("```json\n" + json.dumps(payload) + "\n```")
    cluster = result["clusters"][0]
    assert cluster["weight"] == 1.0
    assert cluster["co_viewing"] == "personal"
    assert cluster["representative_titles"] == ["Broadchurch"]


def test_parse_structured_profile_response_rejects_invalid_json():
    try:
        parse_structured_profile_response("not json")
    except ValueError as exc:
        assert "structured profile JSON" in str(exc)
    else:
        raise AssertionError("invalid JSON did not raise ValueError")


def test_parse_structured_profile_response_deprioritizes_family_weight_once():
    payload = {
        "clusters": [
            {
                "label": "Family animation",
                "weight": 1.0,
                "co_viewing": "family",
            }
        ]
    }

    result = parse_structured_profile_response(json.dumps(payload))
    revalidated = validate_structured_profile(result)

    assert result["clusters"][0]["weight"] == 0.75
    assert revalidated["clusters"][0]["weight"] == 0.75


def test_save_and_load_does_not_reapply_family_weight_penalty(tmp_path):
    profile = parse_structured_profile_response(json.dumps({
        "clusters": [
            {
                "label": "Family animation",
                "weight": 1.0,
                "co_viewing": "family",
            }
        ]
    }))
    path = tmp_path / "taste_profile_structured.json"

    save_structured_profile(profile, path)

    loaded = load_structured_profile(path)
    assert loaded["clusters"][0]["weight"] == 0.75


def test_validate_structured_profile_drops_empty_clusters_and_fills_lists():
    result = validate_structured_profile({
        "clusters": [
            {"label": "British crime", "weight": "0.7"},
            {"label": "   ", "weight": 0.9},
        ]
    })
    assert result["version"] == 1
    assert len(result["clusters"]) == 1
    assert result["clusters"][0]["id"] == "british-crime"
    assert result["clusters"][0]["positive_traits"] == []
    assert result["mood_states"] == []
    assert result["creator_affinities"] == []
    assert result["language_region_affinities"] == []
    assert result["negative_preferences"] == []


def test_build_structured_profile_sends_scores_and_negative_preferences():
    response = json.dumps({
        "version": 1,
        "clusters": [
            {
                "id": "hindi-family",
                "label": "Hindi family dramas",
                "weight": 0.8,
                "positive_traits": ["emotionally direct family stakes"],
                "negative_traits": [],
                "co_viewing": "mixed",
                "mood_states": ["warm"],
                "languages": ["hi"],
                "regions": ["IN"],
                "representative_titles": ["Kabhi Khushi Kabhie Gham"],
            }
        ],
        "negative_preferences": [
            {"label": "generic spectacle", "weight": 0.5, "applies_to": ["action"]}
        ],
    })
    client = make_mock_llm(response)
    profile = build_structured_profile(
        events=[],
        scores={"Kabhi Khushi Kabhie Gham": 0.93, "Unknown": 0.4},
        enrichments={"Kabhi Khushi Kabhie Gham": "Hindi family melodrama."},
        client=client,
        negative_prefs=["Generic Action Movie"],
    )
    prompt = client.generate.call_args[0][0]
    assert "Kabhi Khushi Kabhie Gham (score: 0.93)" in prompt
    assert "Unknown" not in prompt
    assert "Generic Action Movie" in prompt
    assert "ISO-639-1" in prompt
    assert "ISO-3166 alpha-2" in prompt
    assert "computed from members" in prompt
    assert "creator_affinities entries must include weight, traits, and clusters" in prompt
    assert "language_region_affinities entries must include weight, languages, regions, traits, and applies_to" in prompt
    assert "negative_preferences entries must include label, weight and applies_to" in prompt
    assert profile["clusters"][0]["label"] == "Hindi family dramas"


def test_save_and_load_structured_profile_round_trip(tmp_path):
    profile = validate_structured_profile({
        "clusters": [{"label": "British crime", "weight": 0.7}]
    })
    path = tmp_path / "taste_profile_structured.json"
    save_structured_profile(profile, path)
    assert load_structured_profile(path) == profile


def test_load_structured_profile_returns_none_for_missing_or_invalid_file(tmp_path):
    missing = tmp_path / "missing.json"
    assert load_structured_profile(missing) is None
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json")
    assert load_structured_profile(invalid) is None


def test_select_profile_slice_prefers_query_relevant_cluster():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "british-crime",
                "label": "British crime",
                "weight": 0.9,
                "positive_traits": ["patient procedural mystery"],
                "co_viewing": "personal",
                "mood_states": ["serious"],
                "languages": ["en"],
                "regions": ["GB"],
                "representative_titles": ["Broadchurch"],
            },
            {
                "id": "hindi-family",
                "label": "Hindi family dramas",
                "weight": 0.95,
                "positive_traits": ["warm family reconciliation"],
                "co_viewing": "mixed",
                "languages": ["hi"],
                "regions": ["IN"],
                "representative_titles": ["Kabhi Khushi Kabhie Gham"],
            },
        ]
    })
    text = select_profile_slice(
        make_intent(genres=["crime"], origin_countries=["GB"], mood_descriptors=["serious"]),
        profile,
    )
    assert "British crime" in text
    assert "Broadchurch" in text
    assert "Hindi family dramas" not in text


def test_select_profile_slice_suppresses_family_cluster_for_non_family_query():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "family-animation",
                "label": "Family animation",
                "weight": 1.0,
                "positive_traits": ["gentle kid-friendly adventure"],
                "co_viewing": "family",
                "representative_titles": ["Paddington"],
            },
            {
                "id": "adult-thriller",
                "label": "Adult thrillers",
                "weight": 0.5,
                "positive_traits": ["tense moral pressure"],
                "co_viewing": "personal",
                "representative_titles": ["The Night Manager"],
            },
        ]
    })
    text = select_profile_slice(make_intent(genres=["thriller"]), profile)
    assert "Adult thrillers" in text
    assert "Family animation" not in text


def test_select_profile_slice_allows_family_cluster_for_family_query():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "family-animation",
                "label": "Family animation",
                "weight": 1.0,
                "positive_traits": ["gentle kid-friendly adventure"],
                "co_viewing": "family",
                "representative_titles": ["Paddington"],
            }
        ]
    })
    text = select_profile_slice(make_intent(genres=["animation"], special_intent="family"), profile)
    assert "Family animation" in text


def test_select_profile_slice_matches_short_language_and_country_codes_exactly():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "british-investigations",
                "label": "British investigations",
                "weight": 1.0,
                "positive_traits": ["set in London with thin-lipped investigations"],
                "co_viewing": "personal",
                "languages": ["en"],
                "regions": ["GB"],
                "representative_titles": ["Broadchurch"],
            },
            {
                "id": "hindi-family",
                "label": "Hindi family dramas",
                "weight": 0.8,
                "positive_traits": ["emotionally direct family stakes"],
                "co_viewing": "mixed",
                "languages": ["hi"],
                "regions": ["IN"],
                "representative_titles": ["Kabhi Khushi Kabhie Gham"],
            },
        ]
    })

    hindi_text = select_profile_slice(make_intent(languages=["hi"]), profile, max_clusters=1)
    india_text = select_profile_slice(make_intent(origin_countries=["IN"]), profile, max_clusters=1)

    assert "Hindi family dramas" in hindi_text
    assert "British investigations" not in hindi_text
    assert "Hindi family dramas" in india_text
    assert "British investigations" not in india_text


def test_select_profile_slice_keeps_language_region_affinities_query_relevant():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "C1",
                "label": "British crime drama",
                "weight": 0.95,
                "positive_traits": ["set in London with institutional restraint"],
                "co_viewing": "personal",
                "languages": ["en"],
                "regions": ["GB"],
                "representative_titles": ["Broadchurch"],
            },
            {
                "id": "C2",
                "label": "Hindi family drama",
                "weight": 0.80,
                "positive_traits": ["grounded domestic obligation"],
                "co_viewing": "mixed",
                "languages": ["hi"],
                "regions": ["IN"],
                "representative_titles": ["Three of Us"],
            },
        ],
        "creator_affinities": [
            {
                "label": "Taylor Sheridan",
                "weight": 0.85,
                "traits": ["American drama in frontier settings"],
                "clusters": ["C1"],
            },
            {
                "label": "Zoya Akhtar",
                "weight": 0.75,
                "traits": ["Indian social and family dynamics"],
                "clusters": ["C2"],
            },
        ],
        "language_region_affinities": [
            {
                "label": "British English",
                "weight": 0.95,
                "languages": ["en"],
                "regions": ["GB"],
                "traits": ["British drama in regional settings"],
                "applies_to": ["C1"],
            },
            {
                "label": "Hindi and Indian drama",
                "weight": 0.80,
                "languages": ["hi"],
                "regions": ["IN"],
                "traits": ["Hindi domestic drama"],
                "applies_to": ["C2"],
            },
        ],
    })

    text = select_profile_slice(
        make_intent(languages=["hi"], origin_countries=["IN"], genres=["drama"]),
        profile,
        max_clusters=3,
    )

    assert "Hindi family drama" in text
    assert "British crime drama" not in text
    assert "creator affinity: Zoya Akhtar" in text
    assert "creator affinity: Taylor Sheridan" not in text
    assert "language/region affinity: Hindi and Indian drama" in text
    assert "language/region affinity: British English" not in text


def test_select_profile_slice_includes_negative_preferences_for_selected_cluster_case_insensitively():
    profile = validate_structured_profile({
        "clusters": [
            {
                "id": "BritishCrime",
                "label": "British crime",
                "weight": 0.9,
                "positive_traits": ["patient procedural mystery"],
                "co_viewing": "personal",
                "representative_titles": ["Broadchurch"],
            },
        ],
        "negative_preferences": [
            {
                "label": "glossy cop wish fulfillment",
                "weight": 0.7,
                "clusters": ["BritishCrime"],
            },
        ],
    })

    text = select_profile_slice(make_intent(genres=["crime"]), profile)

    assert "negative preference: glossy cop wish fulfillment" in text


def _structured_prompt(monkeypatch, signals, events, scores):
    import config
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", signals)
    client = make_mock_llm(ONE_CLUSTER)
    build_structured_profile(events, scores, {t: "x" for t in scores}, client)
    return client.generate.call_args[0][0]


def test_structured_ties_keep_newest_titles_first(monkeypatch):
    from datetime import datetime, timedelta
    from recommender.ingestion.base import WatchEvent
    events = [
        WatchEvent(platform="netflix", title=f"T{i:03d}", content_type="movie", series_name=f"T{i:03d}",
                   watched_duration=timedelta(minutes=90), total_duration=None,
                   timestamp=datetime(2024, 1, 1) + timedelta(days=i), profile="p")
        for i in range(400)
    ]
    scores = {e.title: 1.0 for e in events}
    prompt = _structured_prompt(monkeypatch, False, events, scores)
    assert "T399" in prompt and "T100" in prompt
    assert "T099" not in prompt and "T000" not in prompt


def test_structured_prompt_wording_follows_setting(monkeypatch):
    off = _structured_prompt(monkeypatch, False, [], {"A": 1.0})
    assert "0.3 for titles known only from a downloads list" in off
    assert "engagement" not in off
    on = _structured_prompt(monkeypatch, True, [], {"A": 0.5})
    assert "engagement scores" in on


def _cluster(label, members, co_viewing="personal"):
    return {"label": label, "co_viewing": co_viewing, "members": members}


def _parse(clusters, scored):
    return parse_structured_profile_response(json.dumps({"version": 1, "clusters": clusters}), scored)


SCORED = [("Line of Duty", 2.0), ("Vera", 2.0), ("Marvel (3 films)", 0.3), ("Cars", 1.0), ("Frozen", 1.0),
          ("Moana", 1.0)]


def test_member_scores_set_cluster_weight_and_order():
    profile = _parse([_cluster("Spectacle", [3]), _cluster("British crime", [1, 2])], SCORED)
    assert [(c["label"], c["weight"]) for c in profile["clusters"]] == [
        ("British crime", 1.0), ("Spectacle", 0.075)]
    assert profile["clusters"][0]["members"] == ["Line of Duty", "Vera"]


def test_bad_member_numbers_are_ignored_and_empty_clusters_sort_last():
    profile = _parse([_cluster("Odd", [0, 99, "x"]), _cluster("Crime", [1, 1]),
                      _cluster("Repeat", [1])], SCORED)
    assert [c["label"] for c in profile["clusters"]] == ["Crime", "Odd", "Repeat"]
    assert profile["clusters"][1]["weight"] == 0.05


def test_family_cluster_sorts_after_personal_even_when_larger():
    profile = _parse([_cluster("Kids", [4, 5, 6], co_viewing="family"), _cluster("Crime", [1])], SCORED)
    assert [c["label"] for c in profile["clusters"]] == ["Crime", "Kids"]


def test_members_survive_save_and_load(tmp_path):
    profile = _parse([_cluster("Crime", [1, 2])], SCORED)
    path = tmp_path / "structured.json"
    save_structured_profile(profile, path)
    assert load_structured_profile(path)["clusters"][0]["members"] == ["Line of Duty", "Vera"]


def test_structured_prompt_numbers_titles_and_asks_for_members(monkeypatch):
    prompt = _structured_prompt(monkeypatch, False, [], {"A": 1.0, "B": 2.0})
    assert "1. B (score: 2.00)" in prompt and "2. A (score: 1.00)" in prompt
    assert "members lists the numbers" in prompt


def test_structured_profile_text_lists_every_cluster_in_order():
    from recommender.structured_profile import structured_profile_text
    profile = _parse([_cluster("Spectacle", [3]), _cluster("British crime", [1, 2])], SCORED)
    text = structured_profile_text(profile)
    assert text.index("British crime") < text.index("Spectacle")
    assert structured_profile_text(None) == ""


def test_whole_profile_prefers_structured_and_falls_back_to_prose():
    from types import SimpleNamespace
    from recommender.query_engine import whole_profile
    profile = _parse([_cluster("British crime", [1])], SCORED)
    assert "British crime" in whole_profile(SimpleNamespace(structured_profile=profile, taste_profile="prose"))
    assert whole_profile(SimpleNamespace(structured_profile=None, taste_profile="prose")) == "prose"


def test_structured_prompt_gives_a_strong_country_or_language_its_own_cluster(monkeypatch):
    prompt = _structured_prompt(monkeypatch, False, [], {"A": 1.0})
    assert "British or Hindi series), give it its own cluster" in prompt


def test_every_loved_title_is_sent_and_descriptions_are_cut(monkeypatch):
    scores = {f"L{i:03d}": 2.0 for i in range(500)} | {f"U{i:03d}": 1.0 for i in range(400)}
    import config
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", False)
    client = make_mock_llm(ONE_CLUSTER)
    build_structured_profile([], scores, {t: "word " * 200 for t in scores}, client)
    prompt = client.generate.call_args[0][0]
    assert all(f"L{i:03d} (score" in prompt for i in range(500))
    assert sum(f"U{i:03d} (score" in prompt for i in range(400)) == 300
    assert "word " * 90 not in prompt


def test_descriptions_drop_the_title_heading_and_blank_lines():
    from recommender.structured_profile import _short
    assert _short("# Line of Duty\n\nA taut British\n\nprocedural.") == "A taut British procedural."


def test_an_answer_with_no_clusters_is_a_failure_and_is_kept_for_diagnosis(tmp_path):
    client = make_mock_llm(json.dumps({"version": 1, "clusters": []}))
    with pytest.raises(ValueError, match="no taste clusters"):
        build_structured_profile([], {"A": 1.0}, {"A": "x"}, client)
    assert '"clusters": []' in (tmp_path / "structured.response.txt").read_text()


def test_clusters_for_one_country_merge_into_one():
    profile = _parse([
        {"label": "British thrillers", "co_viewing": "personal", "regions": ["GB"], "members": [1]},
        {"label": "British cozy mysteries", "co_viewing": "personal", "regions": ["GB"], "members": [2]},
        {"label": "Action", "co_viewing": "personal", "regions": ["US"], "members": [3]},
    ], SCORED)
    labels = [c["label"] for c in profile["clusters"]]
    assert labels == ["British: thrillers; cozy mysteries", "Action"]
    assert profile["clusters"][0]["members"] == ["Line of Duty", "Vera"]


def test_dislikes_come_only_from_the_households_own_titles(monkeypatch):
    client = make_mock_llm(json.dumps({"version": 1, "clusters": [{"label": "Any", "members": [1]}],
                                       "negative_preferences": [{"label": "Bleak drama"}]}))
    profile = build_structured_profile([], {"A": 1.0}, {"A": "x"}, client, negative_prefs=["Scream", "It"])
    assert [n["label"] for n in profile["negative_preferences"]] == ["Titles marked Not for me: Scream, It"]


def test_clusters_keep_their_description():
    profile = _parse([{"label": "Crime", "description": "You love slow-burn detectives.", "members": [1]}], SCORED)
    assert profile["clusters"][0]["description"] == "You love slow-burn detectives."


def test_clusters_keep_a_display_name_apart_from_the_search_label():
    profile = validate_structured_profile({"clusters": [
        {"label": "British cozy mysteries", "name": "Cosy British mysteries"}, {"label": "Docs"}]})
    assert profile["clusters"][0]["label"] == "British cozy mysteries"
    assert profile["clusters"][0]["name"] == "Cosy British mysteries"
    assert profile["clusters"][1]["name"] == ""


def test_merged_country_cluster_keeps_the_name_of_its_biggest_part():
    from recommender.structured_profile import merge_region_clusters
    part = {"co_viewing": "personal", "regions": ["GB"], "description": ""}
    profile = merge_region_clusters({"clusters": [
        {**part, "label": "British thrillers", "name": "Tense British thrillers", "members": ["A"]},
        {**part, "label": "British mysteries", "name": "Cosy British mysteries", "members": ["B", "C"]},
    ]})
    assert len(profile["clusters"]) == 1
    assert profile["clusters"][0]["name"] == "Cosy British mysteries"
    assert "name_size" not in profile["clusters"][0]


def test_prompt_asks_for_names_and_offers_last_names_back(monkeypatch):
    import config
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", False)
    client = make_mock_llm(ONE_CLUSTER)
    build_structured_profile([], {"A": 2.0}, {"A": "x"}, client, previous_names=["Cosy British mysteries"])
    prompt = client.generate.call_args[0][0]
    assert "name is the headline the household sees" in prompt
    assert 'Names used last time: "Cosy British mysteries"' in prompt
    assert "Names used last time" not in _structured_prompt(monkeypatch, False, [], {"A": 2.0})


def test_prompt_states_the_cluster_limit(monkeypatch):
    from recommender.structured_profile import MAX_CLUSTERS
    prompt = _structured_prompt(monkeypatch, False, [], {"A": 2.0})
    assert f"using at most {MAX_CLUSTERS} clusters" in prompt
