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
    assert "not raw watch frequency" in prompt
    assert "creator_affinities entries must include weight, traits, and clusters" in prompt
    assert "language_region_affinities entries must include weight, languages, regions, traits, and applies_to" in prompt
    assert "negative_preferences should include explicit dislikes first" in prompt
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
