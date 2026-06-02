"""Structured logging for the intraday engine (no stray ``print``).

A single helper configures a process-wide logger with a compact, parseable line
format. Synthetic-data runs are labelled unmistakably at the call sites that
emit data provenance (see :mod:`intraday.data`), not here.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def configure_logging(level: int | str | None = None) -> None:
    """Configure root logging once. Idempotent.

    Level resolves from the ``INTRADAY_LOG_LEVEL`` env var, then the ``level``
    argument, then INFO.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    env_level = os.environ.get("INTRADAY_LOG_LEVEL")
    resolved = env_level or level or "INFO"
    logging.basicConfig(level=resolved, format=_DEFAULT_FORMAT)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(name)
