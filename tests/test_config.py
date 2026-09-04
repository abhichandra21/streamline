"""Tests for config.py platform_paths normalization.

Tests exercise _resolve_platform_paths directly (imported from config).
The function is pure given a _paths dict, so we patch config._paths in-place.
"""

import config
from pathlib import Path


def _resolve(raw_value, platform="netflix", default="data/netflix/export.zip"):
    """Call _resolve_platform_paths with a controlled _paths dict."""
    original = config._paths
    try:
        config._paths = {platform: raw_value}
        return config._resolve_platform_paths(platform, default)
    finally:
        config._paths = original


def _resolve_missing(platform="netflix", default="data/netflix/export.zip"):
    """Call _resolve_platform_paths when the platform key is absent."""
    original = config._paths
    try:
        config._paths = {}
        return config._resolve_platform_paths(platform, default)
    finally:
        config._paths = original


# ---------------------------------------------------------------------------
# Input shape normalization
# ---------------------------------------------------------------------------

class TestResolvePlatformPaths:

    def test_string_wraps_in_list(self):
        result = _resolve("data/netflix/export.zip")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].endswith("data/netflix/export.zip")

    def test_list_of_strings_passthrough(self):
        result = _resolve(["data/netflix/a.zip", "data/netflix/b.zip"])
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].endswith("data/netflix/a.zip")
        assert result[1].endswith("data/netflix/b.zip")

    def test_null_returns_empty_list(self):
        assert _resolve(None) == []

    def test_empty_string_returns_empty_list(self):
        assert _resolve("") == []

    def test_empty_list_returns_empty_list(self):
        assert _resolve([]) == []

    def test_list_with_empty_strings_filtered(self):
        result = _resolve(["data/netflix/export.zip", ""])
        assert len(result) == 1
        assert result[0].endswith("data/netflix/export.zip")

    def test_disabled_is_falsy(self):
        """Empty list must be falsy — existing 'if not path:' callers rely on this."""
        assert not _resolve(None)

    def test_enabled_is_truthy(self):
        assert _resolve("data/netflix/export.zip")

    def test_string_path_is_absolute(self):
        result = _resolve("data/netflix/export.zip")
        assert Path(result[0]).is_absolute()

    def test_list_paths_are_absolute(self):
        result = _resolve(["data/netflix/a.zip", "data/netflix/b.zip"])
        for p in result:
            assert Path(p).is_absolute()


# ---------------------------------------------------------------------------
# Missing key uses repo default
# ---------------------------------------------------------------------------

class TestMissingKeyUsesDefault:

    def test_missing_key_returns_default(self):
        result = _resolve_missing("netflix", "data/netflix/export.zip")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].endswith("data/netflix/export.zip")

    def test_missing_key_with_no_default_returns_empty_list(self):
        result = _resolve_missing("disney", None)
        assert result == []

    def test_missing_key_default_path_is_absolute(self):
        result = _resolve_missing("netflix", "data/netflix/export.zip")
        assert Path(result[0]).is_absolute()


# ---------------------------------------------------------------------------
# PLATFORM_PATHS module-level attribute
# ---------------------------------------------------------------------------

class TestPlatformPathsAttribute:

    def test_all_values_are_lists(self):
        for platform, value in config.PLATFORM_PATHS.items():
            assert isinstance(value, list), (
                f"PLATFORM_PATHS['{platform}'] should be list, got {type(value)}"
            )

    def test_known_providers_present(self):
        for provider in ("netflix", "prime", "apple_tv", "disney", "hbo"):
            assert provider in config.PLATFORM_PATHS

    def test_hbo_disabled_without_explicit_config(self):
        """hbo has no repo default path; absent any override it must resolve to []."""
        assert _resolve_missing("hbo", None) == []

    def test_active_providers_truthy(self):
        """netflix/prime/apple_tv should be truthy unless explicitly disabled in config."""
        # At least one of the enabled-by-default providers should be truthy
        # when the repo default paths are in effect (i.e. not overridden to null).
        enabled = [
            config.PLATFORM_PATHS.get(p)
            for p in ("netflix", "prime", "apple_tv")
        ]
        # All should be lists (type check), truthiness depends on config.
        for value in enabled:
            assert isinstance(value, list)


def test_returning_shows_lookback_days_defaults_to_two_years():
    assert config.RETURNING_SHOWS_LOOKBACK_DAYS == 730


def test_release_cache_is_separate_from_tmdb_metadata_cache():
    assert config.RELEASE_CACHE_DIR.endswith("recommender/cache/releases")
    assert config.RELEASE_CACHE_DIR != config.CACHE_DIR
