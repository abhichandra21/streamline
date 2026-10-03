"""Shared pytest fixtures and safety guards for the test suite."""

import pytest

import config
from recommender import imdb_ratings


@pytest.fixture(autouse=True)
def _isolate_tmdb_audit_path(tmp_path, monkeypatch):
    """Redirect TMDB_AUDIT_PATH to a per-test tmp file.

    Without this, any test that exercises the setup pipeline (directly via
    run_setup or indirectly via _audit_cache_mismatches) writes to the real
    recommender/cache/logs/tmdb_audit.txt, clobbering the user's audit
    artifact. The default arg to _audit_cache_mismatches uses
    config.TMDB_AUDIT_PATH, so patching it globally is the safest guard.
    """
    monkeypatch.setattr(
        config,
        "TMDB_AUDIT_PATH",
        str(tmp_path / "tmdb_audit.txt"),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _isolate_imdb_ratings_path(tmp_path, monkeypatch):
    """Point IMDB_RATINGS_DB_PATH at a per-test path that does not exist.

    Without this, tests that run the recommendation pipeline would read the
    user's real recommender/cache/imdb_ratings.db whenever one is present.
    """
    monkeypatch.setattr(config, "IMDB_RATINGS_DB_PATH", str(tmp_path / "imdb_ratings.db"))

    def _no_download(dest):
        raise RuntimeError("IMDb dataset download is disabled in tests")
    monkeypatch.setattr(imdb_ratings, "_download", _no_download)
