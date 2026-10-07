import json

from recommender.franchise import collapse_collections, collection_members


def _cache(tmp_path, films):
    movie_dir = tmp_path / "movie"
    movie_dir.mkdir()
    for tmdb_id, collection in films.items():
        (movie_dir / f"{tmdb_id}.json").write_text(json.dumps({"belongs_to_collection": collection}))
    return tmp_path


XMEN = {"id": 748, "name": "X-Men Collection"}


def test_collection_becomes_one_entry_with_the_best_score(tmp_path):
    cache = _cache(tmp_path, {1: XMEN, 2: XMEN, 3: XMEN})
    scores = {"X-Men": 0.3, "X2": 0.3, "Logan": 2.0}
    enrich = {"X-Men": "mutants", "X2": "more mutants", "Logan": "old mutant"}
    out, text = collapse_collections(scores, {"X-Men": 1, "X2": 2, "Logan": 3}, enrich, cache)
    assert out == {"X-Men Collection (3 films)": 2.0}
    assert text["X-Men Collection (3 films)"].endswith("old mutant")


def test_standalone_and_single_film_collections_pass_through(tmp_path):
    cache = _cache(tmp_path, {1: None, 2: XMEN})
    scores = {"Arrival": 1.0, "X-Men": 1.0}
    out, _ = collapse_collections(scores, {"Arrival": 1, "X-Men": 2}, {}, cache)
    assert out == scores


def test_missing_cache_file_counts_as_no_collection(tmp_path):
    out, _ = collapse_collections({"Film": 1.0}, {"Film": 99}, {}, _cache(tmp_path, {}))
    assert out == {"Film": 1.0}


def test_collection_members_match_collapse_keys(tmp_path):
    cache = _cache(tmp_path, {1: XMEN, 2: XMEN, 3: None})
    scores = {"X-Men": 2.0, "X2": 0.3, "Solo": 1.0}
    ids = {"X-Men": 1, "X2": 2, "Solo": 3}
    collapsed, _ = collapse_collections(scores, ids, {}, cache)
    members = collection_members(list(scores), ids, cache)
    assert set(collapsed) - set(scores) == set(members)
    assert members == {"X-Men Collection (2 films)": ["X-Men", "X2"]}
