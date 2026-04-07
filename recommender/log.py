"""Centralized logging setup.

Configures the root 'recommender' logger to write to both stdout and a
rotating log file at recommender/cache/logs/app.log so that diagnostics
persist across terminal sessions.

The stream handler level is controlled by config.LOG_LEVEL (default WARNING).
The file handler always captures INFO and above regardless of LOG_LEVEL,
so progress events from setup and enrichment are always recorded on disk.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config


def _configure_stream_handler(logger: logging.Logger, level: int, formatter: logging.Formatter) -> None:
    handler = next(
        (h for h in logger.handlers if getattr(h, "_streamline_stream_handler", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._streamline_stream_handler = True
        logger.addHandler(handler)

    handler.setFormatter(formatter)
    handler.setLevel(level)


def _configure_file_handler(logger: logging.Logger, formatter: logging.Formatter) -> None:
    """Add a rotating file handler if not already present.

    The file handler always captures INFO+ so that setup progress and
    enrichment events are persisted regardless of config.LOG_LEVEL.
    """
    if any(getattr(h, "_streamline_file_handler", False) for h in logger.handlers):
        return

    log_path = Path(config.APP_LOG_PATH)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        # If the log directory can't be created (e.g. read-only FS), skip silently.
        return

    handler._streamline_file_handler = True
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def setup_logging(level_override: str | None = None) -> None:
    """Configure logging for all recommender modules.

    Args:
        level_override: If set, overrides config.LOG_LEVEL for stdout (e.g., "DEBUG").
                        The file handler always captures INFO+.
    """
    level_name = level_override or config.LOG_LEVEL
    stream_level = getattr(logging, level_name, logging.WARNING)

    formatter = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("recommender")
    # Set logger gate to INFO so the file handler can receive INFO messages
    # even when the stream handler is at WARNING.
    root.setLevel(logging.INFO)
    root.propagate = False

    _configure_stream_handler(root, stream_level, formatter)
    _configure_file_handler(root, formatter)

    # Route Werkzeug request logs through the same handlers
    werkzeug_log = logging.getLogger("werkzeug")
    werkzeug_log.setLevel(logging.INFO)
    werkzeug_log.propagate = False
    _configure_stream_handler(werkzeug_log, stream_level, formatter)
    _configure_file_handler(werkzeug_log, formatter)
