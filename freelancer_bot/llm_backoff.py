"""Shared cooldown after tokenrouter rate limits.

All LLM-backed components (opportunity analysis, source audit, chat screening)
use the same API key. When one of them receives HTTP 429, every other component
should pause briefly instead of immediately adding more requests.
"""

from __future__ import annotations

import email.utils
import os
import threading
import time
from typing import Any


def _default_cooldown() -> float:
    try:
        return max(1.0, float(os.environ.get("TOKENROUTER_429_COOLDOWN_SECONDS", "30")))
    except ValueError:
        return 30.0


class TokenRouterBackoff:
    def __init__(self, cooldown_seconds: float | None = None) -> None:
        self._cooldown = cooldown_seconds if cooldown_seconds is not None else _default_cooldown()
        self._lock = threading.Lock()
        self._paused_until = 0.0

    def wait(self) -> None:
        """Block while a previous 429 cooldown is still active."""
        with self._lock:
            remaining = self._paused_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def note_rate_limit(self, headers: Any = None) -> None:
        """Extend the shared cooldown after a 429 response."""
        delay = _retry_after_seconds(headers)
        if delay is None:
            delay = self._cooldown
        delay = max(1.0, min(delay, 300.0))
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic()) + delay


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return None
    return max(0.0, parsed.timestamp() - time.time())


shared_backoff = TokenRouterBackoff()
