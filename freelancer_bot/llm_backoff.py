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


def _default_min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("TOKENROUTER_MIN_INTERVAL_SECONDS", "0")))
    except ValueError:
        return 0.0


def bearer_headers(model: str, api_key: str) -> dict[str, str]:
    """Return auth headers only for models that are not anonymously free.

    Kilo Gateway serves ``*:free`` models without authentication.  Sending an
    old or placeholder bearer token to those models returns 401, so the header
    must be omitted entirely.
    """

    if model.strip().endswith(":free"):
        return {}
    return {"Authorization": f"Bearer {api_key}"}


class TokenRouterBackoff:
    def __init__(
        self,
        cooldown_seconds: float | None = None,
        min_interval_seconds: float | None = None,
    ) -> None:
        self._cooldown = cooldown_seconds if cooldown_seconds is not None else _default_cooldown()
        self._min_interval = (
            min_interval_seconds
            if min_interval_seconds is not None
            else _default_min_interval()
        )
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self._last_request = 0.0

    def wait(self) -> None:
        """Serialize requests and honor an active 429 cooldown.

        The lock is held through the sleep so concurrent workers cannot all
        wake up and issue a burst at the same instant.
        """
        with self._lock:
            now = time.monotonic()
            delay = max(
                self._paused_until - now,
                self._last_request + self._min_interval - now,
            )
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

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
