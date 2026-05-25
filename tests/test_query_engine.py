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


def test_rank_candidates_omits_low_score_query_mismatches():
    candidates = [
        make_meta("The Hound of the Baskervilles", tmdb_id=101, content_type="movie", genres=["Mystery"]),
        make_meta("Spirited Away", tmdb_id=129, content_type="movie", genres=["Animation", "Fantasy"]),
    ]
    enrichments = {
        "movie/101": "Sherlock Holmes detective mystery.",
        "movie/129": "Japanese animated fantasy.",
    }
    ranked = [
        {
            "title": "The Hound of the Baskervilles",
            "explanation": "A genuine Holmes-adjacent detective mystery.",
            "score": 0.91,
        },
        {
            "title": "Spirited Away",
            "explanation": "A weak query match included only to fill the list.",
            "score": 0.32,
        },
    ]
    client = make_mock_llm(json.dumps(ranked))

    results = rank_candidates(
        "Suggest me 10 movies like Sherlock Holmes",
        "taste profile",
        candidates,
        enrichments,
        client,
        top_n=10,
    )

    assert [r.title for r in results] == ["The Hound of the Baskervilles"]


def test_rank_candidates_prompt_allows_fewer_than_top_n():
    candidates = [make_meta("The Hound of the Baskervilles", tmdb_id=101)]
    enrichments = {"tv/101": "Detective mystery."}
    ranked = [
        {
            "title": "The Hound of the Baskervilles",
            "explanation": "A strong match.",
            "score": 0.9,
        }
    ]
    client = make_mock_llm(json.dumps(ranked))

    rank_candidates("movies like Sherlock Holmes", "profile", candidates, enrichments, client, top_n=10)

    prompt = client.generate.call_args.args[0]
    assert "Return up to 10 ranked candidates" in prompt
    assert "Never include weak matches just to fill the requested count." in prompt
    assert "Return EXACTLY" not in prompt


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
        watch_index=WatchIndex(tmdb_ids={1}, tmdb_keys={("tv", 1)}, normalized_titles={("broadchurch", "tv")}, entries=[]),
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
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
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
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
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
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
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
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
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


def test_ask_similar_to_only_uses_related_candidates_not_generic_discover():
    seed = make_meta("Sherlock Holmes", tmdb_id=10528, content_type="movie", genres=["Mystery"])
    related = make_meta(
        "The Hound of the Baskervilles",
        tmdb_id=101,
        content_type="movie",
        genres=["Mystery", "Crime"],
        vote_avg=7.4,
    )
    generic = make_meta(
        "Spirited Away",
        tmdb_id=129,
        content_type="movie",
        genres=["Animation", "Fantasy"],
        vote_avg=8.5,
    )

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = [generic]
    mock_tmdb.get_metadata.return_value = seed
    mock_tmdb.get_related_titles.return_value = [related]

    intent_json = json.dumps({
        "genres": [], "origin_countries": [], "languages": [],
        "mood_descriptors": [], "similar_to": ["Sherlock Holmes"],
        "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None,
        "content_type": "movie", "top_n": 10, "platforms": [],
    })
    suggestions_json = json.dumps([])
    ranked_json = json.dumps([
        {
            "title": "The Hound of the Baskervilles",
            "explanation": "A genuine Holmes-adjacent detective mystery.",
            "score": 0.91,
        }
    ])
    mock_llm = make_mock_llm_sequence([intent_json, suggestions_json, ranked_json])

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=mock_llm,
        cache_dir="/tmp/test_cache",
    )

    with patch("recommender.query_engine.enrich_batch", return_value={"movie/101": "Detective mystery."}):
        results = ask("Suggest me 10 movies like Sherlock Holmes", ctx)

    assert [r.title for r in results] == ["The Hound of the Baskervilles"]
    mock_tmdb.search_by_filters.assert_not_called()
    mock_tmdb.get_related_titles.assert_called_once_with(10528, "movie", size=30)


def test_ask_both_content_types_keeps_tv_and_movie_with_same_tmdb_id():
    """TMDB IDs are not globally unique; a TV show and movie can share one."""
    shared_id = 999
    tv_meta = make_meta("Shared ID Show", tmdb_id=shared_id, content_type="tv")
    movie_meta = make_meta("Shared ID Movie", tmdb_id=shared_id, content_type="movie")

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.side_effect = [
        [tv_meta],    # tv Discover call
        [movie_meta], # movie Discover call
    ]
    mock_tmdb.get_metadata.return_value = None

    intent_json = json.dumps({
        "genres": ["drama"], "origin_countries": [], "languages": [],
        "mood_descriptors": [], "similar_to": [],
        "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None,
        "content_type": "both", "top_n": 10, "platforms": [],
    })
    suggestions_json = json.dumps([])
    ranked_json = json.dumps([
        {"title": "Shared ID Show", "explanation": "Good TV.", "score": 0.85},
        {"title": "Shared ID Movie", "explanation": "Good movie.", "score": 0.82},
    ])
    mock_llm = make_mock_llm_sequence([intent_json, suggestions_json, ranked_json])

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=mock_llm,
        cache_dir="/tmp/test_cache",
    )

    with patch("recommender.query_engine.enrich_batch", return_value={
        "tv/999": "Drama series.", "movie/999": "Drama film.",
    }):
        results = ask("drama", ctx)

    titles = [r.title for r in results]
    assert "Shared ID Show" in titles
    assert "Shared ID Movie" in titles


def test_ask_llm_suggestions_respect_content_type():
    """get_metadata alt-type fallback must not inject TV shows into a movie query."""
    tv_via_alttype = make_meta("Houdini & Doyle", tmdb_id=65879, content_type="tv")

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = []
    mock_tmdb.get_metadata.return_value = tv_via_alttype

    intent_json = json.dumps({
        "genres": ["mystery"], "origin_countries": [], "languages": [],
        "mood_descriptors": [], "similar_to": [],
        "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None,
        "content_type": "movie", "top_n": 5, "platforms": [],
    })
    suggestions_json = json.dumps(["Houdini & Doyle"])
    ranked_json = json.dumps([])
    mock_llm = make_mock_llm_sequence([intent_json, suggestions_json, ranked_json])

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=mock_llm,
        cache_dir="/tmp/test_cache",
    )

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        results = ask("mystery movies", ctx)

    assert results == []
    assert all(r.content_type == "movie" for r in results)


def make_structured_prompt_context(taste_profile, structured_profile, llm_responses):
    meta = make_meta("Broadchurch", tmdb_id=77, content_type="tv")
    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = [meta]
    mock_tmdb.get_metadata.return_value = None
    return RecommendContext(
        taste_profile=taste_profile,
        structured_profile=structured_profile,
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        llm=make_mock_llm_sequence(llm_responses),
        cache_dir="/tmp/test_cache",
    )


def test_rank_path_uses_structured_profile_slice_when_available():
    ctx = make_structured_prompt_context(
        taste_profile="FULL PROSE PROFILE WITH IRRELEVANT FAMILY ANIMATION",
        structured_profile={
            "version": 1,
            "clusters": [
                {
                    "id": "british-crime",
                    "label": "British crime",
                    "weight": 0.9,
                    "positive_traits": ["patient procedural mystery"],
                    "negative_traits": [],
                    "co_viewing": "personal",
                    "mood_states": ["serious"],
                    "languages": ["en"],
                    "regions": ["GB"],
                    "representative_titles": ["Broadchurch"],
                },
                {
                    "id": "family-animation",
                    "label": "Family animation",
                    "weight": 1.0,
                    "positive_traits": ["gentle kid-friendly adventure"],
                    "negative_traits": [],
                    "co_viewing": "family",
                    "mood_states": [],
                    "languages": ["en"],
                    "regions": ["US"],
                    "representative_titles": ["Paddington"],
                },
            ],
            "mood_states": [],
            "creator_affinities": [],
            "language_region_affinities": [],
            "negative_preferences": [],
        },
        llm_responses=[
            json.dumps({
                "genres": ["crime"],
                "origin_countries": ["GB"],
                "languages": [],
                "mood_descriptors": ["serious"],
                "similar_to": [],
                "max_runtime_minutes": None,
                "year_from": None,
                "year_to": None,
                "unwatched_only": True,
                "special_intent": None,
                "content_type": "tv",
                "top_n": 1,
                "platforms": [],
            }),
            json.dumps(["Broadchurch"]),
            json.dumps([{
                "title": "Broadchurch",
                "explanation": "Fits serious British crime.",
                "score": 0.92,
            }]),
        ],
    )
    with patch("recommender.query_engine.enrich_batch", return_value={"tv/77": "British coastal crime."}):
        ask("serious British crime", ctx)
    prompts = [call.args[0] for call in ctx.llm.generate.call_args_list]
    combined = "\n\n".join(prompts)
    assert "Relevant taste profile slice:" in combined
    assert "British crime" in combined
    assert "Family animation" not in combined
    assert "FULL PROSE PROFILE WITH IRRELEVANT FAMILY ANIMATION" not in combined


def test_rank_path_falls_back_to_prose_profile_without_structured_profile():
    ctx = make_structured_prompt_context(
        taste_profile="FULL PROSE PROFILE",
        structured_profile=None,
        llm_responses=[
            json.dumps({
                "genres": ["crime"],
                "origin_countries": [],
                "languages": [],
                "mood_descriptors": [],
                "similar_to": [],
                "max_runtime_minutes": None,
                "year_from": None,
                "year_to": None,
                "unwatched_only": True,
                "special_intent": None,
                "content_type": "tv",
                "top_n": 1,
                "platforms": [],
            }),
            json.dumps(["Broadchurch"]),
            json.dumps([{
                "title": "Broadchurch",
                "explanation": "Fits the query.",
                "score": 0.9,
            }]),
        ],
    )
    with patch("recommender.query_engine.enrich_batch", return_value={"tv/77": "British coastal crime."}):
        ask("crime", ctx)
    prompts = [call.args[0] for call in ctx.llm.generate.call_args_list]
    assert any("FULL PROSE PROFILE" in prompt for prompt in prompts)
