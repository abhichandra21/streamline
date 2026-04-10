from unittest.mock import MagicMock

from recommender.tmdb_client import TmdbMetadata
from recommender.enricher import enrich, enrich_batch
from tests.mock_llm import make_mock_llm


def make_meta(title, tmdb_id=42, content_type="tv", genres=None, keywords=None):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=genres or ["Drama"], keywords=keywords or ["mystery"],
        cast=["Actor One"], creator_or_director="Director One",
        original_language="en", vote_average=8.0, vote_count=500,
    )


def test_enrich_calls_haiku(tmp_path):
    meta = make_meta("Broadchurch")
    client = make_mock_llm("A slow-burn British crime drama set in a coastal town.")
    result = enrich(meta, str(tmp_path), client)
    assert "slow-burn" in result
    assert client.generate.call_count == 1


def test_enrich_caches_result(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_llm("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "tv" / "99.txt"
    assert cache_file.exists()
    assert "slow-burn" in cache_file.read_text()


def test_enrich_uses_cache_on_second_call(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_llm("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    enrich(meta, str(tmp_path), client)
    assert client.generate.call_count == 1


def test_enrich_fallback_on_api_error(tmp_path):
    meta = make_meta("Broadchurch", genres=["Crime", "Drama"], keywords=["mystery"])
    client = make_mock_llm("")
    client.generate.side_effect = Exception("API error")
    result = enrich(meta, str(tmp_path), client)
    assert "Crime" in result or "mystery" in result


def test_enrich_unknown_title_uses_slug_cache(tmp_path):
    meta = TmdbMetadata(
        tmdb_id=None, content_type="movie", title="My Unknown Film",
        genres=["Drama"], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )
    client = make_mock_llm("An unknown film.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "unknown" / "my-unknown-film.txt"
    assert cache_file.exists()


def test_enrich_batch_skips_failed(tmp_path):
    meta1 = make_meta("Show A", tmdb_id=1)
    meta2 = make_meta("Show B", tmdb_id=2)
    client = make_mock_llm("")
    client.generate.side_effect = ["Good description.", Exception("fail")]
    result = enrich_batch({"Show A": meta1, "Show B": meta2}, str(tmp_path), client)
    assert "tv/1" in result
    assert "tv/2" in result
    assert result["tv/1"] == "Good description."


def test_enrichment_key_for_movie():
    from recommender.enricher import enrichment_key
    meta = TmdbMetadata(
        tmdb_id=601337, content_type="movie", title="83",
        genres=["Drama"], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )
    assert enrichment_key(meta) == "movie/601337"


def test_enrichment_key_from_parts_unknown():
    from recommender.enricher import enrichment_key_from_parts
    assert enrichment_key_from_parts("movie", None, "Some Title!") == "unknown/some-title"


def test_is_identity_enrichment_index_true():
    from recommender.enricher import is_identity_enrichment_index
    assert is_identity_enrichment_index({"movie/601337": "Desc", "tv/1396": "Desc2"})


def test_is_identity_enrichment_index_false_for_slash_title():
    from recommender.enricher import is_identity_enrichment_index
    assert not is_identity_enrichment_index({"Frost/Nixon": "Desc"})


def test_is_identity_enrichment_index_empty():
    from recommender.enricher import is_identity_enrichment_index
    assert is_identity_enrichment_index({})
