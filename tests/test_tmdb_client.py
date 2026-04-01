import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from recommender.tmdb_client import TmdbClient, TmdbMetadata


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
            with patch.object(client, "_search", return_value=42):
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
