import json
from unittest.mock import MagicMock, patch

import pytest

from recommender.query_engine import (
    QueryIntent, parse_intent, RecommendContext, ask, rank_candidates,
    _parse_json_response, _generate_suggestions,
)
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


def test_ask_hard_excludes_titles(monkeypatch):
    """exclude_titles must drop matching candidates from the pool."""
    import recommender.query_engine as qe

    def _no_parse(*a, **k):
        raise AssertionError("parse_intent must not run with intent_override")
    monkeypatch.setattr(qe, "parse_intent", _no_parse)

    cand = make_meta("Excluded Movie", tmdb_id=1, content_type="movie")

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return [cand]
        def get_metadata(self, title, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"  # no LLM suggestions

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = qe.QueryIntent(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=5, platforms=[],
    )
    # Excluding the only candidate empties the pool -> early empty return.
    results = qe.ask("q", ctx, intent_override=intent,
                     exclude_titles={"Excluded Movie"})
    assert results == []


def test_ask_caps_candidate_pool_before_enrichment(monkeypatch):
    """A broad query must not enrich more than MAX_ENRICH_CANDIDATES titles."""
    import recommender.query_engine as qe

    monkeypatch.setattr(qe.config, "MAX_ENRICH_CANDIDATES", 5)

    def _no_parse(*a, **k):
        raise AssertionError("parse_intent must not run with intent_override")
    monkeypatch.setattr(qe, "parse_intent", _no_parse)

    cands = [make_meta(f"M{i}", tmdb_id=i, content_type="movie", vote_avg=8.0)
             for i in range(1, 21)]

    captured = {}

    def fake_enrich(meta_dict, cache_dir, client):
        captured["n"] = len(meta_dict)
        return {}
    monkeypatch.setattr(qe, "enrich_batch", fake_enrich)
    monkeypatch.setattr(qe, "rank_candidates", lambda *a, **k: [])

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return list(cands)
        def get_metadata(self, title, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = qe.QueryIntent(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=5, platforms=[],
    )
    qe.ask("q", ctx, intent_override=intent)
    assert captured["n"] == 5


def test_ask_trim_reserves_llm_suggestions(monkeypatch):
    """A low-popularity LLM suggestion must survive the trim that otherwise keeps
    only the highest-rated/most-voted candidates."""
    import recommender.query_engine as qe

    monkeypatch.setattr(qe.config, "MAX_ENRICH_CANDIDATES", 5)
    monkeypatch.setattr(qe, "parse_intent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no parse")))

    # 20 wildly popular Discover candidates that would normally crowd out a niche pick.
    cands = [make_meta(f"M{i}", tmdb_id=i, content_type="movie", vote_avg=9.5)
             for i in range(1, 21)]
    for c in cands:
        c.vote_count = 9000
    # Passes the rating gate, but its tiny vote count makes the popularity-weighted
    # sort rank it far below the crowd-pleasers — exactly the case the reserve guards.
    niche = make_meta("Niche Pick", tmdb_id=999, content_type="movie", vote_avg=7.5)
    niche.vote_count = 3
    niche.release_year = 2015

    captured = {}

    def fake_enrich(meta_dict, cache_dir, client):
        captured["titles"] = set(meta_dict.keys())
        return {}
    monkeypatch.setattr(qe, "enrich_batch", fake_enrich)
    monkeypatch.setattr(qe, "rank_candidates", lambda *a, **k: [])

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return list(cands)
        def get_metadata(self, title, ct):
            return niche if title == "Niche Pick" else None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return '["Niche Pick"]'   # the taste-aware suggestion

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = qe.QueryIntent(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=5, platforms=[],
    )
    qe.ask("q", ctx, intent_override=intent)
    assert len(captured["titles"]) == 5
    assert "Niche Pick" in captured["titles"]   # reserved despite low popularity


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
    assert "Return the 10 best. If fewer truly fit, return fewer." in prompt
    assert "Never include weak matches just to fill the count." in prompt
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
        "content_type": "movie", "top_n": 1, "platforms": [],
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
        "content_type": "both", "top_n": 2, "platforms": [],
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


def test_rank_candidates_includes_context_note_in_prompt():
    from recommender import query_engine

    captured = {}

    class FakeLLM:
        provider = "fake"
        def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
            captured["prompt"] = prompt
            return "[]"

    cand = make_meta("Example", tmdb_id=1, content_type="movie")
    query_engine.rank_candidates(
        "something", "PROFILE", [cand], {}, FakeLLM(), top_n=1,
        context_note="Wants something short and low-energy tonight.",
    )
    assert "short and low-energy" in captured["prompt"]


def test_ask_with_intent_override_skips_parse_intent(monkeypatch):
    from recommender import query_engine
    from recommender.query_engine import QueryIntent, RecommendContext

    def boom(*a, **k):
        raise AssertionError("parse_intent must not be called when intent_override is given")
    monkeypatch.setattr(query_engine, "parse_intent", boom)

    class FakeTmdb:
        def search_by_filters(self, **k):
            return []
    class FakeLLM:
        provider = "fake"
        def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
            return "[]"   # no LLM suggestions

    ctx = RecommendContext(
        taste_profile="PROFILE", watch_index=None, tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = QueryIntent(
        genres=[], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=3, platforms=[],
    )
    results = query_engine.ask("ignored", ctx, intent_override=intent,
                               context_note="low energy")
    assert results == []   # no candidates -> empty, but parse_intent never ran


def _run_ask_capturing_enriched(monkeypatch, candidates, intent):
    """Run ask() with intent_override and return the titles that survived filtering.

    Captures the candidate pool handed to enrichment, which is the set of titles
    that passed candidate eligibility (including any runtime filter).
    """
    import recommender.query_engine as qe

    def _no_parse(*a, **k):
        raise AssertionError("parse_intent must not run with intent_override")
    monkeypatch.setattr(qe, "parse_intent", _no_parse)

    captured = {}

    def fake_enrich(meta_dict, cache_dir, client):
        captured["titles"] = list(meta_dict.keys())
        return {}
    monkeypatch.setattr(qe, "enrich_batch", fake_enrich)
    monkeypatch.setattr(qe, "rank_candidates", lambda *a, **k: [])

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return list(candidates)
        def get_metadata(self, title, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    qe.ask("q", ctx, intent_override=intent)
    return captured.get("titles", [])


def _runtime_intent(content_type, max_runtime_minutes):
    return QueryIntent(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=max_runtime_minutes, year_from=None,
        year_to=None, unwatched_only=True, special_intent=None,
        content_type=content_type, top_n=5, platforms=[],
    )


def test_safe_query_intent_falls_back_on_invalid_content_type():
    from recommender.query_engine import _safe_query_intent
    intent = _safe_query_intent({"content_type": "audiobook"})
    assert intent.content_type == "both"


def test_safe_query_intent_keeps_valid_content_type():
    from recommender.query_engine import _safe_query_intent
    assert _safe_query_intent({"content_type": "movie"}).content_type == "movie"


def test_safe_query_intent_coerces_string_runtime():
    from recommender.query_engine import _safe_query_intent
    intent = _safe_query_intent({"max_runtime_minutes": "90"})
    assert intent.max_runtime_minutes == 90


def test_safe_query_intent_drops_invalid_runtime():
    from recommender.query_engine import _safe_query_intent
    intent = _safe_query_intent({"max_runtime_minutes": "soon"})
    assert intent.max_runtime_minutes is None


def test_ask_filters_movies_over_max_runtime(monkeypatch):
    long_movie = make_meta("Long Movie", tmdb_id=1, content_type="movie")
    long_movie.runtime_minutes = 150
    short_movie = make_meta("Short Movie", tmdb_id=2, content_type="movie")
    short_movie.runtime_minutes = 80

    titles = _run_ask_capturing_enriched(
        monkeypatch, [long_movie, short_movie], _runtime_intent("movie", 90))

    assert "Short Movie" in titles
    assert "Long Movie" not in titles


def test_ask_allows_tv_episode_under_max_runtime(monkeypatch):
    # TmdbMetadata.runtime_minutes for TV is episode runtime, not series length.
    episode = make_meta("Half Hour Show", tmdb_id=3, content_type="tv")
    episode.runtime_minutes = 45

    titles = _run_ask_capturing_enriched(
        monkeypatch, [episode], _runtime_intent("tv", 60))

    assert "Half Hour Show" in titles


def test_ask_handles_unknown_runtime_conservatively(monkeypatch):
    # Unknown runtime cannot be safely filtered; keep the candidate.
    unknown = make_meta("Unknown Runtime", tmdb_id=4, content_type="movie")
    unknown.runtime_minutes = None

    titles = _run_ask_capturing_enriched(
        monkeypatch, [unknown], _runtime_intent("movie", 90))

    assert "Unknown Runtime" in titles


def test_ask_relaxes_runtime_when_all_candidates_exceed_max(monkeypatch):
    # "movie under an hour" would otherwise drop every feature film. Rather than
    # return nothing, keep the pool so the user still sees the closest matches.
    long_one = make_meta("Long One", tmdb_id=1, content_type="movie")
    long_one.runtime_minutes = 130
    long_two = make_meta("Long Two", tmdb_id=2, content_type="movie")
    long_two.runtime_minutes = 140

    titles = _run_ask_capturing_enriched(
        monkeypatch, [long_one, long_two], _runtime_intent("movie", 60))

    assert set(titles) == {"Long One", "Long Two"}


def test_ask_runtime_fallback_adds_ranker_hint(monkeypatch):
    import recommender.query_engine as qe

    def _no_parse(*a, **k):
        raise AssertionError("parse_intent must not run with intent_override")
    monkeypatch.setattr(qe, "parse_intent", _no_parse)

    captured = {}
    monkeypatch.setattr(qe, "enrich_batch", lambda *a, **k: {})

    def fake_rank(*args, **kwargs):
        captured["context_note"] = kwargs.get("context_note")
        return []
    monkeypatch.setattr(qe, "rank_candidates", fake_rank)

    class FakeIndex:
        def is_watched(self, c):
            return False

    long_movie = make_meta("Long Only", tmdb_id=1, content_type="movie")
    long_movie.runtime_minutes = 130

    class FakeTmdb:
        def search_by_filters(self, **k):
            return [long_movie]
        def get_metadata(self, title, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    qe.ask("q", ctx, intent_override=_runtime_intent("movie", 60),
           context_note="low energy")

    note = (captured["context_note"] or "").lower()
    assert "runtime" in note
    assert "low energy" in note   # original context preserved


# --- _parse_json_response robustness ---------------------------------------

def test_parse_json_response_plain_json():
    assert _parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_response_fenced_block():
    assert _parse_json_response('```json\n[1, 2]\n```') == [1, 2]


def test_parse_json_response_fenced_block_with_trailing_note():
    raw = (
        '```json\n'
        '[{"title": "Slow Horses"}]\n'
        '```\n\n'
        'Note: I ranked these by how closely they match the British crime brief.'
    )
    assert _parse_json_response(raw) == [{"title": "Slow Horses"}]


def test_parse_json_response_bare_json_with_trailing_note():
    raw = '[{"title": "Slow Horses"}]\n\nNote: only five candidates were plausible.'
    assert _parse_json_response(raw) == [{"title": "Slow Horses"}]


def test_parse_json_response_leading_preamble():
    raw = 'Here are the rankings:\n\n[{"title": "Slow Horses"}]'
    assert _parse_json_response(raw) == [{"title": "Slow Horses"}]


def test_parse_json_response_object_with_trailing_note():
    raw = '{"genres": ["crime"]}\n\nNote: assumed TV given "drama".'
    assert _parse_json_response(raw) == {"genres": ["crime"]}


def test_parse_json_response_rejects_non_json():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response("I could not produce a ranking for this query.")


def test_parse_json_response_preamble_containing_bracket():
    raw = 'Here are the rankings [best first]:\n\n[{"title": "Slow Horses"}]'
    assert _parse_json_response(raw) == [{"title": "Slow Horses"}]


def test_parse_json_response_preamble_containing_brace():
    raw = 'Ranked by fit (see note {below}):\n{"genres": ["crime"]}'
    assert _parse_json_response(raw) == {"genres": ["crime"]}


def test_parse_json_response_rejects_non_json_with_brackets():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response("I could not find matches [sorry].")


def _imdb_db(path, rows):
    import gzip
    from recommender.imdb_ratings import refresh

    def download(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as f:
            f.write("tconst\taverageRating\tnumVotes\n")
            for imdb_id, rating, votes in rows:
                f.write(f"{imdb_id}\t{rating}\t{votes}\n")
    refresh(path, download=download)


def test_attach_imdb_ratings_fills_known_titles_and_resolves_missing_ids(tmp_path):
    import recommender.query_engine as qe
    db = tmp_path / "imdb.db"
    _imdb_db(db, [("tt0000001", 8.2, 151638), ("tt0000002", 6.7, 39079)])
    movie = make_meta("Has ID", tmdb_id=1, content_type="movie", vote_avg=6.0)
    movie.imdb_id = "tt0000001"
    show = make_meta("Needs ID", tmdb_id=2, content_type="tv", vote_avg=7.0)
    unrated = make_meta("Not on IMDb", tmdb_id=3, content_type="movie", vote_avg=5.5)

    class FakeTmdb:
        def __init__(self):
            self.asked = []
        def get_imdb_id(self, tmdb_id, ct):
            self.asked.append((tmdb_id, ct))
            return {2: "tt0000002"}.get(tmdb_id)

    tmdb = FakeTmdb()
    qe.attach_imdb_ratings([movie, show, unrated], tmdb, db_path=str(db))

    assert sorted(tmdb.asked) == [(2, "tv"), (3, "movie")]
    assert (movie.rating, movie.rating_source) == (8.2, "imdb")
    assert (show.rating, show.rating_votes, show.rating_source) == (6.7, 39079, "imdb")
    assert (unrated.rating, unrated.rating_source) == (5.5, "tmdb")


def test_attach_imdb_ratings_without_local_copy_makes_no_calls(tmp_path):
    import recommender.query_engine as qe
    meta = make_meta("A", tmdb_id=1, content_type="tv")

    class NoCalls:
        def get_imdb_id(self, *a):
            raise AssertionError("must not resolve IDs without a ratings copy")

    qe.attach_imdb_ratings([meta], NoCalls(), db_path=str(tmp_path / "missing.db"))
    assert meta.rating_source == "tmdb"


def test_rank_prompt_names_rating_source():
    import recommender.query_engine as qe
    rated = make_meta("Rated", tmdb_id=1, vote_avg=6.0)
    rated.imdb_rating, rated.imdb_votes = 8.2, 151638
    plain = make_meta("Plain", tmdb_id=2, vote_avg=7.1)
    prompts = []

    class FakeLLM:
        provider = "fake"
        def generate(self, prompt, **k):
            prompts.append(prompt)
            return '[{"title": "Rated", "explanation": "x", "score": 0.9}]'

    results = qe.rank_candidates("q", "P", [rated, plain], {}, FakeLLM(), top_n=2)

    assert "Rated, year unknown, TV series, IMDb 8.2 from 151,638 votes" in prompts[0]
    assert "Plain, year unknown, TV series, TMDB 7.1 from 500 votes" in prompts[0]
    assert (results[0].vote_average, results[0].rating_source) == (8.2, "imdb")


def test_ask_min_rating_uses_imdb_rating_when_known(monkeypatch, tmp_path):
    import recommender.query_engine as qe
    db = tmp_path / "imdb.db"
    _imdb_db(db, [("tt0000001", 5.0, 24367), ("tt0000002", 8.6, 32329)])
    monkeypatch.setattr(qe.config, "IMDB_RATINGS_DB_PATH", str(db))
    monkeypatch.setattr(qe.config, "MIN_RATING", 7.0)

    # TMDB says the first is good and the second is poor; IMDb says the reverse.
    tmdb_high = make_meta("TMDB High", tmdb_id=1, content_type="movie", vote_avg=7.5)
    tmdb_high.imdb_id = "tt0000001"
    tmdb_low = make_meta("TMDB Low", tmdb_id=2, content_type="movie", vote_avg=6.0)
    tmdb_low.imdb_id = "tt0000002"
    no_imdb = make_meta("No IMDb", tmdb_id=3, content_type="movie", vote_avg=7.2)

    captured = {}
    monkeypatch.setattr(qe, "enrich_batch",
                        lambda meta_dict, *a: captured.setdefault("titles", set(meta_dict)) and {})
    monkeypatch.setattr(qe, "rank_candidates", lambda *a, **k: [])

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return [tmdb_high, tmdb_low, no_imdb]
        def get_metadata(self, title, ct):
            return None
        def get_imdb_id(self, tmdb_id, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = qe.QueryIntent(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=5, platforms=[],
    )
    qe.ask("q", ctx, intent_override=intent)

    assert captured["titles"] == {"TMDB Low", "No IMDb"}


def test_ask_rating_floor_runs_before_runtime_fallback(monkeypatch):
    """A below-floor short title must not win the runtime filter and then be
    dropped, leaving nothing; the runtime fallback keeps the longer match."""
    import recommender.query_engine as qe
    monkeypatch.setattr(qe.config, "MIN_RATING", 7.0)
    short = make_meta("Short Low", tmdb_id=1, content_type="movie", vote_avg=5.0)
    short.runtime_minutes = 50
    long_ = make_meta("Long High", tmdb_id=2, content_type="movie", vote_avg=8.0)
    long_.runtime_minutes = 130

    captured = {}
    monkeypatch.setattr(qe, "enrich_batch",
                        lambda meta_dict, *a: captured.setdefault("titles", set(meta_dict)) and {})
    monkeypatch.setattr(qe, "rank_candidates", lambda *a, **k: [])

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def search_by_filters(self, **k):
            return [short, long_]
        def get_metadata(self, title, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "[]"

    ctx = qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    qe.ask("q", ctx, intent_override=_runtime_intent("movie", 60))

    assert captured["titles"] == {"Long High"}


def _rated_lookup_ctx(qe, meta, tmp_path, monkeypatch, events=()):
    """Context whose TMDB lookup returns meta and whose IMDb copy rates it 5.0."""
    db = tmp_path / "imdb.db"
    _imdb_db(db, [("tt0000009", 5.0, 24367)])
    monkeypatch.setattr(qe.config, "IMDB_RATINGS_DB_PATH", str(db))
    meta.imdb_id = "tt0000009"

    class FakeIndex:
        def is_watched(self, c):
            return False

    class FakeTmdb:
        def get_metadata(self, title, ct):
            return meta if ct == meta.content_type else None
        def get_imdb_id(self, tmdb_id, ct):
            return None

    class FakeLLM:
        provider = "fake"
        def generate(self, *a, **k):
            return "Yes, keep going."

    return qe.RecommendContext(
        taste_profile="P", watch_index=FakeIndex(), tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir=str(tmp_path), events=list(events),
    )


def test_why_not_rating_exit_card_matches_its_explanation(monkeypatch, tmp_path):
    import recommender.query_engine as qe
    monkeypatch.setattr(qe.config, "MIN_RATING", 7.0)
    meta = make_meta("Bhooth Bangla", tmdb_id=9, content_type="movie", vote_avg=7.5)
    ctx = _rated_lookup_ctx(qe, meta, tmp_path, monkeypatch)

    [rec] = qe._handle_why_not("Bhooth Bangla", ctx)

    assert "IMDb 5.0 from 24,367 votes (minimum 7.0)" in rec.explanation
    assert (rec.vote_average, rec.rating_source) == (5.0, "imdb")


def test_why_not_early_exit_reports_imdb_rating(monkeypatch, tmp_path):
    import recommender.query_engine as qe
    meta = make_meta("Bhooth Bangla", tmdb_id=9, content_type="movie", vote_avg=7.5)
    ctx = _rated_lookup_ctx(qe, meta, tmp_path, monkeypatch)
    ctx.watch_index.is_watched = lambda c: True

    [rec] = qe._handle_why_not("Bhooth Bangla", ctx)

    assert "WATCHED" in rec.explanation
    assert (rec.vote_average, rec.rating_source) == (5.0, "imdb")


def test_abandoned_result_reports_imdb_rating(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    import recommender.query_engine as qe
    from recommender.ingestion.base import WatchEvent
    monkeypatch.setattr(qe, "enrich", lambda *a, **k: "desc")
    meta = make_meta("Mirzapur", tmdb_id=9, content_type="tv", vote_avg=7.9)
    event = WatchEvent(platform="prime", title="Mirzapur: S1E1", content_type="tv",
                       series_name="Mirzapur", watched_duration=timedelta(hours=1),
                       total_duration=None, timestamp=datetime(2026, 1, 1), profile="me")
    ctx = _rated_lookup_ctx(qe, meta, tmp_path, monkeypatch, events=[event])
    intent = qe.QueryIntent(
        genres=[], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=["Mirzapur"], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent="abandoned", content_type="tv",
        top_n=1, platforms=[],
    )

    [rec] = qe._handle_abandoned("q", intent, ctx)

    assert (rec.vote_average, rec.rating_source) == (5.0, "imdb")


# ── Refill, request limits, and fresher picks (#118) ────────────────────────

def _request_intent(**overrides):
    fields = dict(
        genres=["drama"], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=3, platforms=[],
    )
    fields.update(overrides)
    return QueryIntent(**fields)


def _request_ctx(tmdb, llm):
    return RecommendContext(
        taste_profile="P",
        watch_index=WatchIndex(tmdb_ids=set(), tmdb_keys=set(), normalized_titles=set(), entries=[]),
        events=[], tmdb_client=tmdb, llm=llm, cache_dir="/tmp/test_cache",
    )


def _ranked(*titles):
    return json.dumps([{"title": t, "explanation": "fits", "score": 0.9} for t in titles])


def test_ask_refills_when_ranking_leaves_too_few():
    movies = [make_meta(f"Movie {i}", tmdb_id=i, content_type="movie") for i in (1, 2, 3)]
    tmdb = MagicMock()
    tmdb.search_by_filters.side_effect = [movies[:1], movies]   # second call reaches further down
    tmdb.get_metadata.return_value = None
    # suggestions, rank, refill suggestions, rank of the new candidates only
    llm = make_mock_llm_sequence(["[]", _ranked("Movie 1"), "[]", _ranked("Movie 2", "Movie 3")])

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        results = ask("q", _request_ctx(tmdb, llm), intent_override=_request_intent())

    assert {r.title for r in results} == {"Movie 1", "Movie 2", "Movie 3"}
    sizes = [c.kwargs["size"] for c in tmdb.search_by_filters.call_args_list]
    assert sizes == [30, 60]
    calls = llm.generate.call_args_list
    assert "Do not suggest any of these" in calls[2].args[0]
    assert "Movie 1" in calls[2].args[0]
    assert "Movie 1" not in calls[3].args[0]     # already tried, not ranked again


def test_ask_refill_is_capped_at_two_extra_rounds():
    tmdb = MagicMock()
    tmdb.search_by_filters.return_value = []
    ids = {"A": 1, "B": 2, "C": 3, "D": 4}
    tmdb.get_metadata.side_effect = lambda t, ct: make_meta(t, tmdb_id=ids[t], content_type="movie")
    # Each pass finds one new title that the ranker rejects: three suggest + rank pairs.
    llm = make_mock_llm_sequence(['["A"]', "[]", '["B"]', "[]", '["C"]', "[]"])

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        ask("q", _request_ctx(tmdb, llm), intent_override=_request_intent(top_n=10))

    assert tmdb.search_by_filters.call_count == 3
    assert llm.generate.call_count == 6


def test_ask_empty_refill_round_does_not_end_the_loop():
    tmdb = MagicMock()
    tmdb.search_by_filters.return_value = []
    ids = {"A": 1, "B": 2}
    tmdb.get_metadata.side_effect = lambda t, ct: make_meta(t, tmdb_id=ids[t], content_type="movie")
    # Round 1 only re-suggests A (already tried, nothing new); round 2 still runs and finds B.
    llm = make_mock_llm_sequence(['["A"]', "[]", '["A"]', '["B"]', "[]"])

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        ask("q", _request_ctx(tmdb, llm), intent_override=_request_intent(top_n=10))

    assert llm.generate.call_count == 5


def test_ask_stops_refilling_when_sources_are_exhausted():
    tmdb = MagicMock()
    tmdb.search_by_filters.return_value = []
    tmdb.get_metadata.return_value = None
    llm = make_mock_llm("[]")

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        ask("q", _request_ctx(tmdb, llm), intent_override=_request_intent(top_n=10))

    assert llm.generate.call_count == 2          # first suggestions, one empty refill, then stop


def test_ask_related_titles_respect_requested_type_and_years():
    seed = make_meta("Seed", tmdb_id=10, content_type="movie")
    tv = make_meta("A Show", tmdb_id=20, content_type="tv")
    old = make_meta("Old Film", tmdb_id=30, content_type="movie")
    old.release_year = 1980
    good = make_meta("Good Film", tmdb_id=40, content_type="movie")
    good.release_year = 2005
    tmdb = MagicMock()
    tmdb.get_metadata.side_effect = lambda title, ct: seed if title == "Seed" else None
    tmdb.get_related_titles.return_value = [tv, old, good]
    llm = make_mock_llm_sequence(["[]", "[]", "[]", "[]"])
    captured = {}

    def fake_enrich(meta_dict, *a, **k):
        captured.update(meta_dict)
        return {}

    with patch("recommender.query_engine.enrich_batch", side_effect=fake_enrich):
        ask("q", _request_ctx(tmdb, llm),
            intent_override=_request_intent(similar_to=["Seed"], year_from=1990, year_to=2010, top_n=1))

    assert list(captured) == ["Good Film"]


def test_ask_llm_suggestions_respect_requested_years():
    outside = make_meta("Too New", tmdb_id=50, content_type="movie")
    outside.release_year = 2022
    tmdb = MagicMock()
    tmdb.search_by_filters.return_value = []
    tmdb.get_metadata.return_value = outside
    llm = make_mock_llm(json.dumps(["Too New"]))

    with patch("recommender.query_engine.enrich_batch", return_value={}) as enrich:
        results = ask("q", _request_ctx(tmdb, llm),
                      intent_override=_request_intent(year_to=2010, top_n=1))

    assert results == []
    enrich.assert_not_called()



def test_rank_prompt_shows_year_and_type_so_films_differ_from_series():
    film = make_meta("Big Film", tmdb_id=1, content_type="movie")
    film.release_year = 2008
    llm = make_mock_llm("[]")

    rank_candidates("movies", "P", [film], {}, llm, 1)

    prompt = llm.generate.call_args.args[0]
    assert "Big Film, 2008, film," in prompt
    assert "feature films, not TV movies" in prompt


def test_suggestion_prompt_asks_for_forty_well_known_titles():
    llm = make_mock_llm("[]")
    _generate_suggestions("spy movies", "P", llm)
    prompt = llm.generate.call_args.args[0]
    assert "Name 40 real titles" in prompt
    assert "no TV movies, specials" in prompt


def test_suggestion_prompt_carries_refinement_notes():
    llm = make_mock_llm("[]")
    _generate_suggestions("q", "P", llm, context_note="Prefer lesser-known picks.")
    assert "Prefer lesser-known picks." in llm.generate.call_args.args[0]


def test_refill_never_relaxes_the_runtime_limit():
    long_a = make_meta("Long A", tmdb_id=1, content_type="movie")
    long_b = make_meta("Long B", tmdb_id=2, content_type="movie")
    long_a.runtime_minutes = long_b.runtime_minutes = 120
    tmdb = MagicMock()
    tmdb.search_by_filters.side_effect = [[long_a], [long_a, long_b], [long_a, long_b]]
    tmdb.get_metadata.return_value = None
    # Only the first round may relax the 60-minute ceiling; Long B must not be ranked.
    llm = make_mock_llm_sequence(["[]", _ranked("Long A"), "[]", "[]"])

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        results = ask("q", _request_ctx(tmdb, llm),
                      intent_override=_request_intent(top_n=2, max_runtime_minutes=60))

    assert [r.title for r in results] == ["Long A"]
    assert llm.generate.call_count == 4     # no rank call for the refill batch


def test_refill_never_falls_back_to_off_platform_titles():
    a = make_meta("Off A", tmdb_id=1, content_type="movie")
    b = make_meta("Off B", tmdb_id=2, content_type="movie")
    tmdb = MagicMock()
    tmdb.search_by_filters.side_effect = [[a], [a, b], [a, b]]
    tmdb.get_metadata.return_value = None
    tmdb.get_watch_providers.return_value = []
    llm = make_mock_llm_sequence(["[]", _ranked("Off A"), "[]", _ranked("Off B"), "[]"])
    ctx = _request_ctx(tmdb, llm)
    ctx.providers_cache_dir = "/tmp/test_providers"

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        results = ask("q", ctx, intent_override=_request_intent(top_n=2, platforms=["Netflix"]))

    # The first round falls back to unfiltered; the refill round keeps nothing off-platform.
    assert [r.title for r in results] == ["Off A"]


def test_refill_prompt_names_suggestions_that_were_filtered_out():
    gone = make_meta("Gone Film", tmdb_id=7, content_type="movie")
    tmdb = MagicMock()
    tmdb.search_by_filters.return_value = []
    tmdb.get_metadata.return_value = gone
    llm = make_mock_llm_sequence(['["Gone Film"]', "[]"])
    ctx = _request_ctx(tmdb, llm)
    ctx.watch_index = WatchIndex(tmdb_ids={7}, tmdb_keys={("movie", 7)}, normalized_titles=set(), entries=[])

    with patch("recommender.query_engine.enrich_batch", return_value={}):
        ask("q", ctx, intent_override=_request_intent())

    assert "Gone Film" in llm.generate.call_args_list[1].args[0]
