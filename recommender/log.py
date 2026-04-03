"""Centralized logging setup.

Configures the root 'recommender' logger to write to stdout so that
systemd journal (or any process supervisor) captures output natively.
"""

import logging
import sys

import config


def setup_logging(level_override: str | None = None) -> None:
    """Configure logging for all recommender modules.

    Args:
        level_override: If set, overrides config.LOG_LEVEL (e.g., "DEBUG").
    """
    level_name = level_override or config.LOG_LEVEL
    level = getattr(logging, level_name, logging.WARNING)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger("recommender")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False

    # Route Werkzeug request logs through the same handler
    werkzeug_log = logging.getLogger("werkzeug")
    werkzeug_log.setLevel(level)
    werkzeug_log.addHandler(handler)
    werkzeug_log.propagate = False
