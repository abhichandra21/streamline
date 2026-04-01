import pytest
from pathlib import Path
from unittest.mock import MagicMock

from recommender.tmdb_client import TmdbMetadata
from recommender.enricher import enrich, enrich_batch


def make_meta(title, tmdb_id=42, content_type="tv", genres=None, keywords=None):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=genres or ["Drama"], keywords=keywords or ["mystery"],
        cast=["Actor One"], creator_or_director="Director One",
        original_language="en", vote_average=8.0, vote_count=500,
    )


def make_mock_client(response_text: str):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = msg
    return client


def test_enrich_calls_haiku(tmp_path):
    meta = make_meta("Broadchurch")
    client = make_mock_client("A slow-burn British crime drama set in a coastal town.")
    result = enrich(meta, str(tmp_path), client)
    assert "slow-burn" in result
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_enrich_caches_result(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_client("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "tv" / "99.txt"
    assert cache_file.exists()
    assert "slow-burn" in cache_file.read_text()


def test_enrich_uses_cache_on_second_call(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_client("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    enrich(meta, str(tmp_path), client)
    assert client.messages.create.call_count == 1


def test_enrich_fallback_on_api_error(tmp_path):
    meta = make_meta("Broadchurch", genres=["Crime", "Drama"], keywords=["mystery"])
    client = MagicMock()
    client.messages.create.side_effect = Exception("API error")
    result = enrich(meta, str(tmp_path), client)
    assert "Crime" in result or "mystery" in result


def test_enrich_unknown_title_uses_slug_cache(tmp_path):
    meta = TmdbMetadata(
        tmdb_id=0, content_type="movie", title="My Unknown Film",
        genres=["Drama"], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )
    client = make_mock_client("An unknown film.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "unknown" / "my-unknown-film.txt"
    assert cache_file.exists()


def test_enrich_batch_skips_failed(tmp_path):
    meta1 = make_meta("Show A", tmdb_id=1)
    meta2 = make_meta("Show B", tmdb_id=2)
    client = MagicMock()
    good_msg = MagicMock()
    good_msg.content = [MagicMock(text="Good description.")]
    client.messages.create.side_effect = [good_msg, Exception("fail")]
    result = enrich_batch({"Show A": meta1, "Show B": meta2}, str(tmp_path), client)
    assert "Show A" in result
    assert "Show B" in result  # fallback used
    assert result["Show A"] == "Good description."
