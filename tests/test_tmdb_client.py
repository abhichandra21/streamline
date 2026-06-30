import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from recommender.tmdb_client import (
    TmdbClient, TmdbMetadata, MatchHints, _is_plausible_title_match, _title_similarity,
)


def make_client(tmp_dir):
    return TmdbClient(api_key="test_key", cache_dir=tmp_dir)


def fake_tv_response():
    return {
        "id": 42,
        "name": "Avatar: The Last Airbender",
        "original_language": "en",
        "vote_average": 9.2,
        "vote_count": 5000,
        "genres": [{"id": 1, "name": "Animation"}, {"id": 2, "name": "Adventure"}],
        "keywords": {"results": [{"id": 1, "name": "martial arts"}, {"id": 2, "name": "magic"}]},
        "credits": {"cast": [{"name": "Zach Tyler"}, {"name": "Mae Whitman"}], "crew": []},
        "created_by": [{"name": "Michael DiMartino"}],
        "episode_run_time": [23],
    }


def fake_movie_response():
    return {
        "id": 99,
        "title": "14 Peaks: Nothing Is Impossible",
        "original_language": "en",
        "vote_average": 7.8,
        "vote_count": 1200,
        "runtime": 101,
        "genres": [{"id": 3, "name": "Documentary"}],
        "keywords": {"keywords": [{"id": 5, "name": "mountaineering"}]},
        "credits": {
            "cast": [{"name": "Nirmal Purja"}],
            "crew": [{"job": "Director", "name": "Torquil Jones"}],
        },
        "created_by": [],
    }


def test_parse_tv_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        meta = client._parse_metadata(fake_tv_response(), "tv")
        assert meta.title == "Avatar: The Last Airbender"
        assert meta.content_type == "tv"
        assert "Animation" in meta.genres
        assert "Adventure" in meta.genres
        assert "martial arts" in meta.keywords
        assert meta.creator_or_director == "Michael DiMartino"
        assert meta.runtime_minutes == 23
        assert meta.vote_average == 9.2
        assert meta.vote_count == 5000


def test_parse_movie_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        meta = client._parse_metadata(fake_movie_response(), "movie")
        assert meta.title == "14 Peaks: Nothing Is Impossible"
        assert meta.content_type == "movie"
        assert "Documentary" in meta.genres
        assert meta.creator_or_director == "Torquil Jones"
        assert meta.runtime_minutes == 101


def test_cache_hit_avoids_api_call():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        # Pre-populate cache
        client._save_cache("tv", 42, fake_tv_response())

        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = Exception("Should not call API")
            with patch.object(client, "_ranked_search", return_value=42):
                meta = client.get_metadata("Avatar: The Last Airbender", "tv")

        assert meta is not None
        assert meta.title == "Avatar: The Last Airbender"


def test_search_miss_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        with patch.object(client, "_get") as mock_get:
            mock_get.return_value = {"results": []}
            meta = client.get_metadata("Nonexistent Title XYZ123", "tv")
        assert meta is None


def test_clear_cache():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        client._save_cache("tv", 42, fake_tv_response())
        assert client._cache_path("tv", 42).exists()
        client.clear_cache()
        assert not client._cache_path("tv", 42).exists()


def test_genre_maps_have_crime():
    from recommender.tmdb_client import MOVIE_GENRE_IDS, TV_GENRE_IDS
    assert 'crime' in MOVIE_GENRE_IDS
    assert 'crime' in TV_GENRE_IDS
    assert MOVIE_GENRE_IDS['crime'] == 80
    assert TV_GENRE_IDS['crime'] == 80


def test_genre_maps_have_drama():
    from recommender.tmdb_client import MOVIE_GENRE_IDS, TV_GENRE_IDS
    assert 'drama' in MOVIE_GENRE_IDS
    assert 'drama' in TV_GENRE_IDS
    assert MOVIE_GENRE_IDS['drama'] == 18


def test_search_by_filters_calls_discover():
    from recommender.tmdb_client import MOVIE_GENRE_IDS, TV_GENRE_IDS
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        discover_response = {"results": [{"id": 1}, {"id": 2}]}
        details_response = {
            "id": 1, "name": "Test Show", "genres": [{"name": "Crime"}],
            "keywords": {"results": []}, "credits": {"cast": [], "crew": []},
            "created_by": [], "episode_run_time": [45],
            "original_language": "en", "vote_average": 8.0, "vote_count": 500,
        }

        with patch.object(client, '_get') as mock_get:
            mock_get.side_effect = [discover_response, details_response, details_response]
            results = client.search_by_filters(
                content_type="tv",
                genres=["crime"],
                origin_countries=["GB"],
                size=2,
            )

        discover_call = mock_get.call_args_list[0]
        assert discover_call[0][0] == "discover/tv"
        assert "80" in discover_call[1]['params']['with_genres']
        assert discover_call[1]['params']['with_origin_country'] == "GB"


def test_search_by_filters_movie():
    from recommender.tmdb_client import MOVIE_GENRE_IDS, TV_GENRE_IDS
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        discover_response = {"results": [{"id": 10}]}
        details_response = {
            "id": 10, "title": "Test Movie", "genres": [{"name": "Drama"}],
            "keywords": {"keywords": []}, "credits": {"cast": [], "crew": []},
            "runtime": 120, "original_language": "hi",
            "vote_average": 7.5, "vote_count": 300,
        }

        with patch.object(client, '_get') as mock_get:
            mock_get.side_effect = [discover_response, details_response]
            results = client.search_by_filters(
                content_type="movie",
                languages=["hi"],
                size=1,
            )

        discover_call = mock_get.call_args_list[0]
        assert discover_call[0][0] == "discover/movie"
        assert discover_call[1]['params']['with_original_language'] == "hi"


def test_get_related_titles_merges_recommendations_and_similar():
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        recommendations_response = {"results": [{"id": 101}, {"id": 102}]}
        similar_response = {"results": [{"id": 102}, {"id": 103}]}

        detail_101 = {
            "id": 101, "title": "The Hound of the Baskervilles",
            "genres": [{"name": "Mystery"}],
            "keywords": {"keywords": []}, "credits": {"cast": [], "crew": []},
            "runtime": 100, "original_language": "en",
            "vote_average": 7.2, "vote_count": 300,
            "release_date": "2002-01-01",
        }
        detail_102 = {
            "id": 102, "title": "Without a Clue",
            "genres": [{"name": "Comedy"}, {"name": "Mystery"}],
            "keywords": {"keywords": []}, "credits": {"cast": [], "crew": []},
            "runtime": 107, "original_language": "en",
            "vote_average": 7.0, "vote_count": 350,
            "release_date": "1988-10-21",
        }
        detail_103 = {
            "id": 103, "title": "Young Sherlock Holmes",
            "genres": [{"name": "Adventure"}, {"name": "Mystery"}],
            "keywords": {"keywords": []}, "credits": {"cast": [], "crew": []},
            "runtime": 109, "original_language": "en",
            "vote_average": 6.8, "vote_count": 400,
            "release_date": "1985-12-04",
        }

        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                recommendations_response,
                detail_101,
                detail_102,
                similar_response,
                detail_103,
            ]
            results = client.get_related_titles(10528, "movie", size=3)

    assert [r.tmdb_id for r in results] == [101, 102, 103]
    assert [r.title for r in results] == [
        "The Hound of the Baskervilles",
        "Without a Clue",
        "Young Sherlock Holmes",
    ]
    endpoints = [call.args[0] for call in mock_get.call_args_list]
    assert endpoints == [
        "movie/10528/recommendations",
        "movie/101",
        "movie/102",
        "movie/10528/similar",
        "movie/103",
    ]


# --- Candidate ranking tests ---

def test_title_similarity_exact():
    assert _title_similarity("Honeyland", "Honeyland") == 1.0


def test_title_similarity_case_insensitive():
    assert _title_similarity("honeyland", "Honeyland") == 1.0


def test_title_similarity_articles_stripped():
    assert _title_similarity("The Matrix", "Matrix") == 1.0


def test_title_similarity_low_for_mismatch():
    sim = _title_similarity("Kesari", "The King")
    assert sim < 0.5


def _make_search_result(tmdb_id, title, release_date="", poster_path=None,
                        vote_count=100, popularity=20):
    return {
        "id": tmdb_id,
        "title": title,
        "release_date": release_date,
        "poster_path": poster_path,
        "vote_count": vote_count,
        "popularity": popularity,
    }


def _make_tv_search_result(tmdb_id, name, first_air_date="", poster_path=None,
                           vote_count=100, popularity=20):
    return {
        "id": tmdb_id,
        "name": name,
        "first_air_date": first_air_date,
        "poster_path": poster_path,
        "vote_count": vote_count,
        "popularity": popularity,
    }


def _make_details(tmdb_id, title, runtime=None, release_date=""):
    return {
        "id": tmdb_id,
        "title": title,
        "runtime": runtime,
        "release_date": release_date,
        "genres": [],
        "keywords": {"keywords": []},
        "credits": {"cast": [], "crew": []},
        "original_language": "en",
        "vote_average": 7.0,
        "vote_count": 100,
    }


def test_year_hint_selects_correct_candidate():
    """Iceland with release_year=2016 should pick 2016 over 1942."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        hints = MatchHints(release_year=2016)

        cand_1942 = _make_search_result(111, "Iceland", "1942-01-01", "/poster.jpg")
        cand_2016 = _make_search_result(222, "Iceland", "2016-06-15", "/poster.jpg")

        details_1942 = _make_details(111, "Iceland", runtime=90, release_date="1942-01-01")
        details_2016 = _make_details(222, "Iceland", runtime=95, release_date="2016-06-15")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                if params and params.get("primary_release_year") == 2016:
                    return {"results": [cand_2016]}
                return {"results": [cand_1942, cand_2016]}
            if "111" in endpoint:
                return details_1942
            if "222" in endpoint:
                return details_2016
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Iceland", "movie", hints=hints)

        assert meta is not None
        assert meta.tmdb_id == 222


def test_year_hint_selects_honeyland_2019():
    """Honeyland with release_year=2019 should pick 2019 over 1935."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        hints = MatchHints(release_year=2019)

        cand_1935 = _make_search_result(333, "Honeyland", "1935-01-01", None, vote_count=5, popularity=1)
        cand_2019 = _make_search_result(444, "Honeyland", "2019-02-01", "/poster.jpg", vote_count=800, popularity=30)

        details_1935 = _make_details(333, "Honeyland", runtime=70, release_date="1935-01-01")
        details_2019 = _make_details(444, "Honeyland", runtime=85, release_date="2019-02-01")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                if params and params.get("primary_release_year") == 2019:
                    return {"results": [cand_2019]}
                return {"results": [cand_1935, cand_2019]}
            if "333" in endpoint:
                return details_1935
            if "444" in endpoint:
                return details_2019
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Honeyland", "movie", hints=hints)

        assert meta is not None
        assert meta.tmdb_id == 444


def test_runtime_hint_selects_correct_hum_tum():
    """Hum Tum with runtime ~140 should pick the 142-min movie over the 123-min one."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        hints = MatchHints(runtime_minutes=140, runtime_is_exact=True)

        cand_123 = _make_search_result(555, "Hum Tum", "2004-05-28", "/poster.jpg")
        cand_142 = _make_search_result(556, "Hum Tum", "2004-05-28", "/poster.jpg")

        details_123 = _make_details(555, "Hum Tum", runtime=123, release_date="2004-05-28")
        details_142 = _make_details(556, "Hum Tum", runtime=142, release_date="2004-05-28")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand_123, cand_142]}
            if "555" in endpoint:
                return details_123
            if "556" in endpoint:
                return details_142
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Hum Tum", "movie", hints=hints)

        assert meta is not None
        assert meta.tmdb_id == 556


def test_title_mismatch_deprioritized():
    """Kesari should not match 'The King' despite it being first result."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        cand_king = _make_search_result(700, "The King", "2019-10-11", "/poster.jpg", vote_count=2000)
        cand_kesari = _make_search_result(701, "Kesari", "2019-03-21", "/poster.jpg", vote_count=300)

        details_king = _make_details(700, "The King", runtime=140, release_date="2019-10-11")
        details_kesari = _make_details(701, "Kesari", runtime=150, release_date="2019-03-21")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand_king, cand_kesari]}
            if "700" in endpoint:
                return details_king
            if "701" in endpoint:
                return details_kesari
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Kesari", "movie")

        assert meta is not None
        assert meta.tmdb_id == 701


def test_is_plausible_title_match_checks_localized_and_original_title():
    cand = {"title": "America's Sweethearts", "original_title": "America's Sweethearts"}
    assert not _is_plausible_title_match("Don", cand)

    cand_alias = {"title": "Don 2", "original_title": "डॉन"}
    assert _is_plausible_title_match("Don", cand_alias)


def test_post_search_validator_overrides_implausible_top_match():
    """'Don' must not resolve to an unrelated, more popular candidate just
    because it wins on votes/popularity -- the exact #51 failure mode."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        cand_unrelated = _make_search_result(
            11467, "America's Sweethearts", "2001-07-20", "/poster.jpg",
            vote_count=2000, popularity=40,
        )
        cand_don = _make_search_result(
            555, "Don", "2006-10-20", "/poster.jpg", vote_count=50, popularity=5,
        )

        details_unrelated = _make_details(11467, "America's Sweethearts", runtime=103, release_date="2001-07-20")
        details_don = _make_details(555, "Don", runtime=171, release_date="2006-10-20")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand_unrelated, cand_don]}
            if "11467" in endpoint:
                return details_unrelated
            if "555" in endpoint:
                return details_don
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Don", "movie")

        assert meta is not None
        assert meta.tmdb_id == 555


def test_post_search_validator_keeps_winner_when_no_alternative_is_plausible():
    """If nothing among the candidates plausibly matches, don't change the pick."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        cand_a = _make_search_result(1, "Totally Unrelated", "2001-01-01", vote_count=2000)
        cand_b = _make_search_result(2, "Also Unrelated", "2002-01-01", vote_count=10)

        details_a = _make_details(1, "Totally Unrelated", runtime=100, release_date="2001-01-01")
        details_b = _make_details(2, "Also Unrelated", runtime=110, release_date="2002-01-01")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand_a, cand_b]}
            if endpoint.endswith("/1"):
                return details_a
            if endpoint.endswith("/2"):
                return details_b
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Something Else Entirely", "movie")

        assert meta is not None
        assert meta.tmdb_id == 1


def test_language_hint_boosts_matching_original_language_candidate():
    """Two same-named candidates: the one matching the language hint should win."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        hints = MatchHints(language="hi")

        cand_en = _make_search_result(900, "Goodbye", "2003-01-01", "/poster.jpg", vote_count=500, popularity=10)
        cand_en["original_language"] = "en"
        cand_hi = _make_search_result(901, "Goodbye", "2022-10-07", "/poster.jpg", vote_count=50, popularity=5)
        cand_hi["original_language"] = "hi"

        details_en = _make_details(900, "Goodbye", runtime=120, release_date="2003-01-01")
        details_hi = _make_details(901, "Goodbye", runtime=135, release_date="2022-10-07")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand_en, cand_hi]}
            if "900" in endpoint:
                return details_en
            if "901" in endpoint:
                return details_hi
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Goodbye", "movie", hints=hints)

        assert meta is not None
        assert meta.tmdb_id == 901


def test_no_hints_still_works():
    """get_metadata without hints should still return a result."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)

        cand = _make_search_result(800, "Inception", "2010-07-16", "/poster.jpg")
        details = _make_details(800, "Inception", runtime=148, release_date="2010-07-16")

        def fake_get(endpoint, params=None):
            if "search" in endpoint:
                return {"results": [cand]}
            if "800" in endpoint:
                return details
            return {}

        with patch.object(client, "_get", side_effect=fake_get):
            meta = client.get_metadata("Inception", "movie")

        assert meta is not None
        assert meta.tmdb_id == 800


def test_resolve_title_confident_keeps_same_title_close_results_ambiguous():
    """Same-title TMDB results need a score margin before persisting an ID."""
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp)
        candidates = [
            _make_tv_search_result(
                2316, "The Office", "2001-07-09", "/uk.jpg",
                vote_count=1000, popularity=20,
            ),
            _make_tv_search_result(
                69735, "The Office", "1995-03-11", "/variant.jpg",
                vote_count=1000, popularity=18,
            ),
        ]

        with patch.object(client, "_search_candidates", return_value=candidates):
            tmdb_id, resolved_type = client.resolve_title_confident("The Office", "tv")

        assert tmdb_id is None
        assert resolved_type == "tv"
