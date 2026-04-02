import json
from unittest.mock import MagicMock, patch

from recommender.query_engine import QueryIntent, parse_intent, RecommendContext, ask, rank_candidates
from recommender.tmdb_client import TmdbMetadata
from recommender.watch_index import WatchIndex
from tests.mock_llm import make_mock_llm, make_mock_llm_sequence


BRITISH_CRIME_INTENT = {
    "genres": ["crime", "drama"],
    "origin_countries": ["GB"],
    "languages": [],
    "mood_descriptors": ["slow-burn"],
    "similar_to": [],
    "max_runtime_minutes": None,
    "year_from": None,
    "year_to": None,
    "unwatched_only": True,
    "special_intent": None,
    "content_type": "tv",
    "top_n": 1,
}


def test_parse_intent_returns_query_intent():
    client = make_mock_llm(json.dumps(BRITISH_CRIME_INTENT))
    intent = parse_intent("good British crime drama", client)
    assert isinstance(intent, QueryIntent)


def test_parse_intent_extracts_genres():
    client = make_mock_llm(json.dumps(BRITISH_CRIME_INTENT))
    intent = parse_intent("good British crime drama", client)
    assert "crime" in intent.genres
    assert "drama" in intent.genres


def test_parse_intent_extracts_origin_country():
    client = make_mock_llm(json.dumps(BRITISH_CRIME_INTENT))
    intent = parse_intent("good British crime drama", client)
    assert "GB" in intent.origin_countries


def test_parse_intent_uses_reason_role():
    client = make_mock_llm(json.dumps(BRITISH_CRIME_INTENT))
    parse_intent("good British crime drama", client)
    call_kwargs = client.generate.call_args[1]
    assert call_kwargs["role"] == "reason"


def test_parse_intent_handles_markdown_wrapped_json():
    client = make_mock_llm("```json\n" + json.dumps(BRITISH_CRIME_INTENT) + "\n```")
    intent = parse_intent("good British crime drama", client)
    assert intent.genres == ["crime", "drama"]


def test_parse_intent_abandoned_special():
    abandoned_intent = {**BRITISH_CRIME_INTENT, "special_intent": "abandoned",
                        "genres": [], "origin_countries": [],
                        "similar_to": ["Tandav"], "content_type": "tv", "top_n": 1}
    client = make_mock_llm(json.dumps(abandoned_intent))
    intent = parse_intent("I started Tandav and stopped", client)
    assert intent.special_intent == "abandoned"
    assert "Tandav" in intent.similar_to


def test_parse_intent_bollywood():
    bollywood_intent = {
        "genres": ["romance"], "origin_countries": ["IN"],
        "languages": ["hi"], "mood_descriptors": ["feel-good"],
        "similar_to": [], "max_runtime_minutes": None,
        "year_from": 1990, "year_to": 1999,
        "unwatched_only": True, "special_intent": None, "content_type": "movie",
        "top_n": 1,
    }
    client = make_mock_llm(json.dumps(bollywood_intent))
    intent = parse_intent("feel-good Bollywood romance from the 90s", client)
    assert "hi" in intent.languages
    assert intent.year_from == 1990
    assert intent.content_type == "movie"


def make_meta(title, tmdb_id=1, content_type="tv", genres=None, vote_avg=8.0):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=genres or ["Crime", "Drama"], keywords=[],
        cast=[], creator_or_director=None,
        original_language="en", vote_average=vote_avg, vote_count=500,
    )


def test_rank_candidates_returns_recommendations():
    candidates = [make_meta("Broadchurch", tmdb_id=1), make_meta("Hinterland", tmdb_id=2)]
    enrichments = {"Broadchurch": "Dark coastal crime.", "Hinterland": "Welsh noir."}
    ranked = [
        {"title": "Broadchurch", "explanation": "Fits your taste.", "score": 0.92},
        {"title": "Hinterland", "explanation": "Similar tone.", "score": 0.85},
    ]
    client = make_mock_llm(json.dumps(ranked))
    results = rank_candidates("British crime drama", "taste profile", candidates, enrichments, client, top_n=2)
    assert len(results) == 2
    assert results[0].title == "Broadchurch"
    assert results[0].score == 0.92
    assert "Fits your taste" in results[0].explanation


def test_rank_candidates_uses_reason_role():
    candidates = [make_meta("Broadchurch")]
    enrichments = {"Broadchurch": "Desc."}
    ranked = [{"title": "Broadchurch", "explanation": "Good.", "score": 0.9}]
    client = make_mock_llm(json.dumps(ranked))
    rank_candidates("query", "profile", candidates, enrichments, client)
    call_kwargs = client.generate.call_args[1]
    assert call_kwargs["role"] == "reason"


def test_rank_candidates_skips_unknown_titles():
    candidates = [make_meta("Broadchurch")]
    enrichments = {"Broadchurch": "Desc."}
    ranked = [
        {"title": "Broadchurch", "explanation": "Good.", "score": 0.9},
        {"title": "Phantom Title", "explanation": "Hallucinated.", "score": 0.95},
    ]
    client = make_mock_llm(json.dumps(ranked))
    results = rank_candidates("query", "profile", candidates, enrichments, client)
    titles = [r.title for r in results]
    assert "Phantom Title" not in titles
    assert "Broadchurch" in titles


def test_ask_excludes_watched_titles():
    meta_watched = make_meta("Broadchurch", tmdb_id=1)
    meta_new = make_meta("Hinterland", tmdb_id=2)

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = [meta_watched, meta_new]
    mock_tmdb.get_metadata.return_value = None

    intent_json = json.dumps({
        "genres": ["crime"], "origin_countries": ["GB"], "languages": [],
        "mood_descriptors": [], "similar_to": [], "max_runtime_minutes": None,
        "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None, "content_type": "tv",
        "top_n": 1, "platforms": [],
    })
    suggestions_json = json.dumps(["Shetland", "Vera"])
    ranked_json = json.dumps([
        {"title": "Hinterland", "explanation": "Great fit.", "score": 0.88}
    ])

    mock_llm = make_mock_llm_sequence([intent_json, suggestions_json, ranked_json])

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids={1}, normalized_titles={("broadchurch", "tv")}, entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=mock_llm,
        cache_dir="/tmp/test_cache",
    )

    with patch('recommender.query_engine.enrich_batch', return_value={"Hinterland": "Welsh noir."}):
        results = ask("British crime drama", ctx)

    titles = [r.title for r in results]
    assert "Broadchurch" not in titles
    assert "Hinterland" in titles
