"""Tests for recommender/log.py: file creation, handler deduplication, and log visibility."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_recommender_logger():
    """Remove all handlers from the recommender logger before each test."""
    logger = logging.getLogger("recommender")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate

    logger.handlers.clear()

    yield

    logger.handlers = original_handlers
    logger.level = original_level
    logger.propagate = original_propagate


# ── File creation ─────────────────────────────────────────────────────────────

class TestFileHandlerCreation:
    def test_log_file_created_on_setup(self, tmp_path):
        log_file = tmp_path / "logs" / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()

        assert log_file.exists(), "Log file should be created by setup_logging()"

    def test_log_file_parent_dirs_created(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "logs" / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()

        assert log_file.parent.is_dir()

    def test_missing_log_dir_does_not_raise(self, tmp_path):
        """If the log directory cannot be created, setup_logging should not crash."""
        log_file = tmp_path / "logs" / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            with patch("pathlib.Path.mkdir", side_effect=OSError("no space")):
                from recommender.log import setup_logging
                # Should not raise
                setup_logging()


# ── Handler deduplication ─────────────────────────────────────────────────────

class TestHandlerDeduplication:
    def test_calling_setup_twice_does_not_duplicate_stream_handler(self, tmp_path):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()
            setup_logging()

        logger = logging.getLogger("recommender")
        stream_handlers = [
            h for h in logger.handlers if getattr(h, "_streamline_stream_handler", False)
        ]
        assert len(stream_handlers) == 1, "Stream handler should not be duplicated"

    def test_calling_setup_twice_does_not_duplicate_file_handler(self, tmp_path):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()
            setup_logging()

        logger = logging.getLogger("recommender")
        file_handlers = [
            h for h in logger.handlers if getattr(h, "_streamline_file_handler", False)
        ]
        assert len(file_handlers) == 1, "File handler should not be duplicated"

    def test_file_handler_is_rotating(self, tmp_path):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()

        logger = logging.getLogger("recommender")
        file_handlers = [
            h for h in logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1


# ── Log visibility ────────────────────────────────────────────────────────────

class TestLogVisibility:
    def test_info_message_written_to_file_even_at_warning_stdout_level(self, tmp_path):
        """File handler must capture INFO even when stdout level is WARNING."""
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)), \
             patch("config.LOG_LEVEL", "WARNING"):
            from recommender.log import setup_logging
            setup_logging()

        logger = logging.getLogger("recommender.test_visibility")
        logger.info("hello from info")

        content = log_file.read_text()
        assert "hello from info" in content

    def test_warning_message_written_to_file(self, tmp_path):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)):
            from recommender.log import setup_logging
            setup_logging()

        logger = logging.getLogger("recommender.test_warn")
        logger.warning("a warning message")

        content = log_file.read_text()
        assert "a warning message" in content

    def test_info_message_not_written_to_stdout_at_warning_level(self, tmp_path, capsys):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)), \
             patch("config.LOG_LEVEL", "WARNING"):
            from recommender.log import setup_logging
            setup_logging()

        logger = logging.getLogger("recommender.test_stdout")
        logger.info("info should not reach stdout at warning level")

        captured = capsys.readouterr()
        assert "info should not reach stdout at warning level" not in captured.out

    def test_warning_message_written_to_stdout(self, tmp_path, capsys):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)), \
             patch("config.LOG_LEVEL", "WARNING"):
            from recommender.log import setup_logging
            setup_logging()

        logger = logging.getLogger("recommender.test_stdout_warn")
        logger.warning("warning on stdout")

        captured = capsys.readouterr()
        assert "warning on stdout" in captured.out

    def test_level_override_makes_info_visible_on_stdout(self, tmp_path, capsys):
        log_file = tmp_path / "app.log"
        with patch("config.APP_LOG_PATH", str(log_file)), \
             patch("config.LOG_LEVEL", "WARNING"):
            from recommender.log import setup_logging
            setup_logging(level_override="INFO")

        logger = logging.getLogger("recommender.test_override")
        logger.info("info with override")

        captured = capsys.readouterr()
        assert "info with override" in captured.out


# ── Web UI /logs route ────────────────────────────────────────────────────────

class TestLogsRoute:
    @pytest.fixture
    def client(self):
        from recommender.web import app
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        with app.test_client() as c:
            yield c

    def test_logs_page_returns_200_when_no_log_file(self, client, tmp_path):
        missing = tmp_path / "nonexistent.log"
        with patch("config.APP_LOG_PATH", str(missing)):
            resp = client.get("/logs")
        assert resp.status_code == 200
        assert b"No log entries yet" in resp.data

    def test_logs_page_shows_recent_lines(self, client, tmp_path):
        log_file = tmp_path / "app.log"
        log_file.write_text("line one\nline two\nline three\n")
        with patch("config.APP_LOG_PATH", str(log_file)):
            resp = client.get("/logs")
        assert resp.status_code == 200
        assert b"line one" in resp.data
        assert b"line three" in resp.data

    def test_logs_page_limits_to_200_lines(self, client, tmp_path):
        log_file = tmp_path / "app.log"
        log_file.write_text("\n".join(f"line {i}" for i in range(300)) + "\n")
        with patch("config.APP_LOG_PATH", str(log_file)):
            resp = client.get("/logs")
        assert resp.status_code == 200
        # First 100 lines should not be visible (only last 200 shown)
        assert b"line 0\n" not in resp.data
        assert b"line 299" in resp.data
