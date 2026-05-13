"""Centralized logging setup.

Configures the root 'recommender' logger to write to both stdout and a
rotating log file at logs/app.log so that diagnostics
persist across terminal sessions.

The stream handler level is controlled by config.LOG_LEVEL (default WARNING).
The file handler always captures INFO and above regardless of LOG_LEVEL,
so progress events from setup and enrichment are always recorded on disk.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

import config

# Shared console for both log output and live displays (Progress/status).
# Routing both through the same Console is what lets RichHandler render log
# records *above* an active Progress bar instead of stomping on it.
console = Console(stderr=True)


def _configure_stream_handler(logger: logging.Logger, level: int, formatter: logging.Formatter) -> None:
    handler = next(
        (h for h in logger.handlers if getattr(h, "_streamline_stream_handler", False)),
        None,
    )
    if handler is None:
        handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
            log_time_format="%Y-%m-%d %H:%M:%S",
        )
        handler._streamline_stream_handler = True
        logger.addHandler(handler)

    handler.setLevel(level)


_shared_file_handler: RotatingFileHandler | None = None
_shared_file_path: str | None = None


def _get_file_handler(formatter: logging.Formatter) -> RotatingFileHandler | None:
    """Return the single shared rotating file handler, creating it on first call.

    If config.APP_LOG_PATH changes (e.g. during tests), a new handler is created.
    """
    global _shared_file_handler, _shared_file_path

    log_path_str = str(Path(config.APP_LOG_PATH))
    if _shared_file_handler is not None and _shared_file_path == log_path_str:
        return _shared_file_handler

    log_path = Path(config.APP_LOG_PATH)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None

    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    _shared_file_handler = handler
    _shared_file_path = log_path_str
    return handler


def _configure_file_handler(logger: logging.Logger, formatter: logging.Formatter) -> None:
    """Attach the shared rotating file handler if not already present."""
    handler = _get_file_handler(formatter)
    if handler is None or handler in logger.handlers:
        return
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
    # Logger gate must be the lowest of stream_level and INFO (file handler),
    # so that DEBUG passes through when --debug is used.
    root.setLevel(min(stream_level, logging.INFO))
    root.propagate = False

    _configure_stream_handler(root, stream_level, formatter)
    _configure_file_handler(root, formatter)

    # Route Werkzeug request logs through the same handlers
    werkzeug_log = logging.getLogger("werkzeug")
    werkzeug_log.setLevel(logging.INFO)
    werkzeug_log.propagate = False
    _configure_stream_handler(werkzeug_log, stream_level, formatter)
    _configure_file_handler(werkzeug_log, formatter)
