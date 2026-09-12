from __future__ import annotations

import logging
import sys

import structlog

from asm.config import settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.log_level.upper()
    )

    # httpx logs the full request URL at INFO, and Helius puts the API key in the
    # query string - so at INFO the key is written to logs/*.log in plaintext.
    # Nothing here needs httpx's per-request line; we log our own.
    for noisy in ("httpx", "httpcore", "hpack", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str):
    setup_logging()
    return structlog.get_logger(name)
