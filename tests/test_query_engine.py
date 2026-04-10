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
    enrichments = {"tv/1": "Dark coastal crime.", "tv/2": "Welsh noir."}
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
    enrichments = {"tv/1": "Desc."}
    ranked = [{"title": "Broadchurch", "explanation": "Good.", "score": 0.9}]
    client = make_mock_llm(json.dumps(ranked))
    rank_candidates("query", "profile", candidates, enrichments, client)
    call_kwargs = client.generate.call_args[1]
    assert call_kwargs["role"] == "reason"


def test_rank_candidates_skips_unknown_titles():
    candidates = [make_meta("Broadchurch")]
    enrichments = {"tv/1": "Desc."}
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

    with patch('recommender.query_engine.enrich_batch', return_value={"tv/2": "Welsh noir."}):
        results = ask("British crime drama", ctx)

    titles = [r.title for r in results]
    assert "Broadchurch" not in titles
    assert "Hinterland" in titles


def test_recommend_context_events_eager():
    """Existing callers passing events=[] still work."""
    mock_llm = make_mock_llm("")
    ctx = RecommendContext(
        taste_profile="profile",
        watch_index=WatchIndex(tmdb_ids=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=MagicMock(),
        llm=mock_llm,
        cache_dir="/tmp",
    )
    assert ctx.events == []


def test_recommend_context_events_lazy_loads_once():
    """Lazy loader is called once, result is cached."""
    call_count = 0

    def loader():
        nonlocal call_count
        call_count += 1
        return ["event1", "event2"]

    mock_llm = make_mock_llm("")
    ctx = RecommendContext(
        taste_profile="profile",
        watch_index=WatchIndex(tmdb_ids=set(), normalized_titles=set(), entries=[]),
        tmdb_client=MagicMock(),
        llm=mock_llm,
        cache_dir="/tmp",
        _events_loader=loader,
    )
    # First access triggers loader
    assert ctx.events == ["event1", "event2"]
    # Second access uses cache
    assert ctx.events == ["event1", "event2"]
    assert call_count == 1


def test_recommend_context_events_no_loader_returns_empty():
    """No events and no loader returns empty list."""
    mock_llm = make_mock_llm("")
    ctx = RecommendContext(
        taste_profile="profile",
        watch_index=WatchIndex(tmdb_ids=set(), normalized_titles=set(), entries=[]),
        tmdb_client=MagicMock(),
        llm=mock_llm,
        cache_dir="/tmp",
    )
    assert ctx.events == []


def test_user_state_excludes_dismissed_and_manual_archive():
    """ask() drops dismissed and manually-watched titles from the candidate pool."""
    meta_dismissed = make_meta("Dismissed Show", tmdb_id=111)
    meta_manual = make_meta("Already Watched", tmdb_id=222)
    meta_new = make_meta("Fresh Pick", tmdb_id=333)

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = [meta_dismissed, meta_manual, meta_new]
    mock_tmdb.get_metadata.return_value = None

    intent_json = json.dumps({
        "genres": ["crime"], "origin_countries": ["GB"], "languages": [],
        "mood_descriptors": [], "similar_to": [], "max_runtime_minutes": None,
        "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None, "content_type": "tv",
        "top_n": 1, "platforms": [],
    })
    suggestions_json = json.dumps([])
    ranked_json = json.dumps([
        {"title": "Fresh Pick", "explanation": "Still eligible.", "score": 0.91}
    ])
    mock_llm = make_mock_llm_sequence([intent_json, suggestions_json, ranked_json])

    class FakeUserState:
        def is_manually_watched(self, meta):
            return meta.tmdb_id == 222

        def is_dismissed(self, meta):
            return meta.tmdb_id == 111

        def has_rating(self, meta):
            return False

        def get_rating(self, meta):
            return None

        def is_in_watchlist(self, meta):
            return False

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=mock_llm,
        cache_dir="/tmp/test_cache",
        user_state=FakeUserState(),
    )

    with patch("recommender.query_engine.enrich_batch", return_value={"tv/333": "Eligible"}):
        results = ask("British crime drama", ctx)

    titles = [r.title for r in results]
    assert "Dismissed Show" not in titles
    assert "Already Watched" not in titles
    assert titles == ["Fresh Pick"]
