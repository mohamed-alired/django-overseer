"""Console logging for the management commands, matching ``db_worker``'s behaviour."""

from __future__ import annotations

import logging

LEVELS = {0: logging.CRITICAL, 1: logging.INFO}


def configure(stdout, verbosity: int) -> None:
    """Give the ``overseer`` logger a level from ``verbosity`` and a console handler when
    the project configured none, so the commands' messages are visible."""
    logger = logging.getLogger("overseer")
    logger.setLevel(LEVELS.get(verbosity, logging.DEBUG))
    if not logger.hasHandlers():
        logger.addHandler(logging.StreamHandler(stdout))
