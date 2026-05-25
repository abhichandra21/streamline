"""Shared pytest fixtures and safety guards for the test suite."""

import pytest

import config


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
