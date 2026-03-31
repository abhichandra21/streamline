from recommender.tmdb_client import TmdbMetadata
from recommender.engine import recommend, Recommendation


def make_meta(tmdb_id, title, genres, vote_avg=7.0, vote_count=500, lang="en", ct="tv"):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=ct, title=title,
        genres=genres, keywords=[], cast=[],
        original_language=lang, vote_average=vote_avg, vote_count=vote_count,
    )


def test_recommend_returns_top_n():
    profile = {"genre:animation": 0.9, "genre:adventure": 0.5}
    candidates = [
        make_meta(i, f"Show {i}", ["Animation", "Adventure"]) for i in range(20)
    ]
    results = recommend("tv", profile, candidates, watched_titles=set(),
                         watched_metadata={}, top_n=5)
    assert len(results) == 5


def test_watched_titles_filtered_out():
    profile = {"genre:drama": 0.8}
    candidates = [
        make_meta(1, "Watched Show", ["Drama"]),
        make_meta(2, "New Show", ["Drama"]),
    ]
    results = recommend("tv", profile, candidates,
                         watched_titles={"Watched Show"},
                         watched_metadata={}, top_n=10)
    titles = [r.title for r in results]
    assert "Watched Show" not in titles
    assert "New Show" in titles


def test_low_vote_count_filtered():
    profile = {"genre:drama": 0.8}
    candidates = [
        make_meta(1, "Popular Show", ["Drama"], vote_count=500),
        make_meta(2, "Obscure Show", ["Drama"], vote_count=10),
    ]
    results = recommend("tv", profile, candidates,
                         watched_titles=set(), watched_metadata={},
                         top_n=10, min_vote_count=100)
    titles = [r.title for r in results]
    assert "Popular Show" in titles
    assert "Obscure Show" not in titles


def test_results_sorted_by_score_descending():
    profile = {"genre:animation": 0.9, "genre:drama": 0.1}
    candidates = [
        make_meta(1, "Drama Show", ["Drama"], vote_count=500),
        make_meta(2, "Animation Show", ["Animation"], vote_count=500),
    ]
    results = recommend("tv", profile, candidates,
                         watched_titles=set(), watched_metadata={}, top_n=10)
    assert results[0].title == "Animation Show"
    assert results[0].score > results[1].score


def test_because_you_watched_populated():
    profile = {"genre:animation": 0.9}
    watched_meta = {
        "Avatar": make_meta(10, "Avatar", ["Animation"]),
    }
    candidates = [make_meta(1, "New Anime", ["Animation"], vote_count=500)]
    results = recommend("tv", profile, candidates,
                         watched_titles=set(),
                         watched_metadata=watched_meta, top_n=10)
    assert results[0].because_you_watched == "Avatar"


def test_recommendation_has_expected_fields():
    profile = {"genre:drama": 0.7}
    candidates = [make_meta(1, "Drama Show", ["Drama"], vote_avg=8.5, vote_count=1000)]
    results = recommend("tv", profile, candidates,
                         watched_titles=set(), watched_metadata={}, top_n=10)
    r = results[0]
    assert isinstance(r, Recommendation)
    assert r.title == "Drama Show"
    assert r.vote_average == 8.5
    assert "Drama" in r.genres
    assert r.score > 0
