"""Shared pytest fixtures and safety guards for the test suite."""

import time

import pytest

import config
from recommender import imdb_ratings, language_catalog
from recommender.jobs import registry as job_registry


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
    yield
    # A route test can start the web UI's background refresh job. Wait for it
    # here, while _download is still stubbed: this teardown runs before
    # monkeypatch restores the real downloader, and a job that reached
    # _download after that would fetch the live dataset.
    deadline = time.monotonic() + 10
    background_labels = {imdb_ratings.REFRESH_JOB_LABEL, language_catalog.BUILD_JOB_LABEL}
    while any(j.label in background_labels for j in job_registry.running_jobs()):
        if time.monotonic() > deadline:
            raise RuntimeError("IMDb background job did not finish within 10 seconds")
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _isolate_find_cache_dir(tmp_path, monkeypatch):
    """Point FIND_CACHE_DIR at a per-test directory.

    The saved language lists live there. Without this, a setup test would see
    the user's real list and, once it was a day old, rebuild it from live TMDB.
    The path keeps its real suffix for tests that check the configured name.
    """
    monkeypatch.setattr(config, "FIND_CACHE_DIR", str(tmp_path / "recommender/cache/find"))


@pytest.fixture(autouse=True)
def _isolate_providers_cache_dir(tmp_path, monkeypatch):
    """Point PROVIDERS_CACHE_DIR at a per-test directory.

    On Deck and the title page read streaming services from it. Without this,
    a route test would show the user's real cached providers.
    """
    monkeypatch.setattr(config, "PROVIDERS_CACHE_DIR", str(tmp_path / "providers"))
