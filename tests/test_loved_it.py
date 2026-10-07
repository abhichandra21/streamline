import json
import random

from recommender.loved_it import Candidate, group_key, load_archive, merge_small_groups, next_set


def c(i, group, real=True, votes=100, ct="movie"):
    return Candidate(ct, i, f"T{i}", 2020, "/p.jpg", group, real, votes)


def test_group_key_puts_region_before_genre():
    assert group_key({"origin_country": ["GB"], "genres": [{"name": "Crime"}]}, "tv") == "British · Crime"
    assert group_key({"origin_country": ["GB"], "genres": [{"name": "Drama"}, {"name": "Mystery"}]},
                     "tv") == "British · Crime"
    assert group_key({"original_language": "hi", "genres": [{"name": "Comedy"}]}, "movie") == "Hindi · Comedy"
    assert group_key({"origin_country": ["US", "GB"], "genres": [{"name": "Action"}]}, "movie") == "Action"
    assert group_key({"production_countries": [{"iso_3166_1": "GB"}], "genres": [{"name": "Comedy"}]},
                     "movie") == "British · Comedy"
    assert group_key({"original_language": "en", "genres": [{"name": "Animation"}]}, "movie") == "Kids & family"
    assert group_key({"original_language": "en", "genres": []}, "movie") == "Other"


def test_small_region_groups_merge_into_their_genre():
    pool = [c(i, "Danish · Romance") for i in range(3)] + [c(10 + i, "British · Crime") for i in range(12)]
    assert {p.group for p in merge_small_groups(pool)} == {"Romance", "British · Crime"}


def test_first_set_spreads_across_every_group():
    pool = [c(g * 100 + i, f"G{g}") for g in range(8) for i in range(10)]
    picked = next_set(pool, {}, size=24, rng=random.Random(1))
    assert len(picked) == 24 and len({p.group for p in picked}) == 8


def test_groups_you_love_get_more_slots_and_cold_groups_fade():
    pool = [c(i, "Loved") for i in range(50)] + [c(100 + i, "Cold") for i in range(50)]
    history = {"Loved": (20, 12), "Cold": (20, 0)}
    groups = [p.group for p in next_set(pool, history, size=24, rng=random.Random(1))]
    assert groups.count("Loved") > 20 and groups.count("Cold") >= 1


def test_best_known_come_first_within_a_group_whatever_the_source():
    pool = [c(1, "G", real=False, votes=9000), c(2, "G", votes=10), c(3, "G", votes=500)]
    assert [p.tmdb_id for p in next_set(pool, {"G": (10, 5)}, size=3, rng=random.Random(1))] == [1, 3, 2]


def test_shows_and_films_alternate_so_shows_are_not_buried():
    pool = [c(1, "G", votes=9000), c(2, "G", votes=8000), c(3, "G", votes=50, ct="tv")]
    assert [p.tmdb_id for p in next_set(pool, {"G": (10, 5)}, size=3, rng=random.Random(1))] == [3, 1, 2]


def test_big_groups_are_explored_first_and_kids_last():
    pool = ([c(i, "Big") for i in range(30)] + [c(100 + i, "Tiny") for i in range(2)]
            + [c(200 + i, "Kids & family") for i in range(40)])
    first = [p.group for p in next_set(pool, {}, size=3, rng=random.Random(1))]
    assert first[0] == "Big" and "Kids & family" not in first[:2]


def test_small_pool_returns_what_is_left():
    assert len(next_set([c(1, "G")], {}, size=24)) == 1


def test_load_archive_skips_titles_without_a_poster_or_id(tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "tv" / "1.json").write_text(json.dumps({
        "poster_path": "/v.jpg", "origin_country": ["GB"], "genres": [{"name": "Crime"}],
        "first_air_date": "2011-05-01", "vote_count": 300}))
    (tmp_path / "tv" / "2.json").write_text(json.dumps({"poster_path": None}))
    entries = [
        {"title": "Vera", "content_type": "tv", "tmdb_id": 1, "platforms": ["manual", "prime"]},
        {"title": "No poster", "content_type": "tv", "tmdb_id": 2, "platforms": ["netflix"]},
        {"title": "No id", "content_type": "movie", "platforms": ["netflix"]},
    ]
    [vera] = load_archive(entries, tmp_path)
    assert (vera.title, vera.year, vera.real, vera.group) == ("Vera", 2011, True, "Crime")
