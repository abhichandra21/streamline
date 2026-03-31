import math
from recommender.tmdb_client import TmdbMetadata
from recommender.taste_profile import build_tag_vector, build_profile


def make_meta(title, genres=None, keywords=None, cast=None, creator=None, lang="en"):
    return TmdbMetadata(
        tmdb_id=1, content_type="tv", title=title,
        genres=genres or [], keywords=keywords or [],
        cast=cast or [], creator_or_director=creator,
        original_language=lang, vote_average=8.0, vote_count=1000,
    )


def test_build_tag_vector_genres():
    meta = make_meta("Test", genres=["Animation", "Adventure"])
    vec = build_tag_vector(meta)
    assert "genre:animation" in vec
    assert "genre:adventure" in vec
    assert vec["genre:animation"] == 1.0


def test_build_tag_vector_keywords_have_higher_weight():
    meta = make_meta("Test", genres=["Drama"], keywords=["time travel"])
    vec = build_tag_vector(meta)
    assert vec["keyword:time travel"] == 1.5
    assert vec["keyword:time travel"] > vec["genre:drama"]


def test_build_tag_vector_language():
    meta = make_meta("Test", lang="hi")
    vec = build_tag_vector(meta)
    assert "lang:hi" in vec
    assert vec["lang:hi"] == 0.8


def test_build_tag_vector_creator():
    meta = make_meta("Test", creator="Rian Johnson")
    vec = build_tag_vector(meta)
    assert "person:rian johnson" in vec
    assert vec["person:rian johnson"] == 0.5


def test_build_profile_accumulates_weights():
    meta = make_meta("Show A", genres=["Animation"])
    scores = {"Show A": 1.0}
    metadata = {"Show A": meta}
    profile = build_profile(scores, metadata)
    assert "genre:animation" in profile
    assert profile["genre:animation"] > 0


def test_build_profile_normalized():
    meta1 = make_meta("Show A", genres=["Animation", "Adventure"])
    meta2 = make_meta("Show B", genres=["Drama"])
    scores = {"Show A": 0.9, "Show B": 0.3}
    metadata = {"Show A": meta1, "Show B": meta2}
    profile = build_profile(scores, metadata)
    magnitude = math.sqrt(sum(v * v for v in profile.values()))
    assert abs(magnitude - 1.0) < 1e-6


def test_build_profile_skips_missing_metadata():
    scores = {"Known Show": 0.8, "Unknown Show": 0.5}
    meta = make_meta("Known Show", genres=["Drama"])
    metadata = {"Known Show": meta}
    # Should not raise — Unknown Show is silently skipped
    profile = build_profile(scores, metadata)
    assert "genre:drama" in profile


def test_high_scored_title_dominates_profile():
    meta_high = make_meta("High", genres=["Animation"])
    meta_low = make_meta("Low", genres=["Horror"])
    scores = {"High": 1.0, "Low": 0.01}
    metadata = {"High": meta_high, "Low": meta_low}
    profile = build_profile(scores, metadata)
    assert profile.get("genre:animation", 0) > profile.get("genre:horror", 0)
