"""Subsystem loggers for the Telex plugin.

Mirrors openclaw-telex's per-module logging (telex/subscribe, telex/inbound,
telex/outbound, telex/media, telex/backfill, telex/access, telex/pairing).
Never log the API key or message body text.
"""

from __future__ import annotations

import logging

_ROOT = "hermes_telex"


def get_logger(module: str) -> logging.Logger:
    """Return a namespaced logger, e.g. get_logger('subscribe')."""
    return logging.getLogger(f"{_ROOT}.{module}")


def mask_key(api_key: str | None) -> str:
    """Render an API key for logs: only the last 4 chars, never the full value."""
    if not api_key:
        return "<none>"
    tail = api_key[-4:] if len(api_key) >= 4 else "?"
    return f"...{tail}"
