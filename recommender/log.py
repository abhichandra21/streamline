"""Centralized logging setup.

Configures the root 'recommender' logger to write to stdout so that
systemd journal (or any process supervisor) captures output natively.
"""

import logging
import sys

import config


def _configure_named_handler(logger: logging.Logger, level: int, formatter: logging.Formatter) -> None:
    handler = next(
        (existing for existing in logger.handlers if getattr(existing, "_streamline_handler", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._streamline_handler = True
        logger.addHandler(handler)

    handler.setFormatter(formatter)
    logger.setLevel(level)
    logger.propagate = False


def setup_logging(level_override: str | None = None) -> None:
    """Configure logging for all recommender modules.

    Args:
        level_override: If set, overrides config.LOG_LEVEL (e.g., "DEBUG").
    """
    level_name = level_override or config.LOG_LEVEL
    level = getattr(logging, level_name, logging.WARNING)

    formatter = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("recommender")
    _configure_named_handler(root, level, formatter)

    # Route Werkzeug request logs through the same handler
    werkzeug_log = logging.getLogger("werkzeug")
    _configure_named_handler(werkzeug_log, level, formatter)
