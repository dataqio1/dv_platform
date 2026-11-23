"""Structured logging (structlog). Console renderer in local mode, JSON in server/prod."""

from __future__ import annotations

import logging

import structlog

from dvp.config import RuntimeMode, Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)

    renderer = (
        structlog.dev.ConsoleRenderer()
        if settings.profile == RuntimeMode.LOCAL
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_request_context(**values: str) -> None:
    """Bind per-request fields (e.g. request_id) so every log line in this request carries them.
    Works because `configure_logging` installs `merge_contextvars` in the processor chain."""
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    """Drop all contextvars bound for the current request (call at the end of each request)."""
    structlog.contextvars.clear_contextvars()
