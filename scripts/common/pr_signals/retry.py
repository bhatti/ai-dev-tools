"""Retry with backoff for tracker calls: rate limits (429, GitHub secondary limits) and transient 5xx."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

ATTEMPTS = 4
BASE_DELAY = 1.0
MAX_DELAY = 30.0


class Retryable(Exception):
    """A call failed in a way worth retrying; ``delay`` is the server's Retry-After when it sent one."""

    def __init__(self, message: str, delay: float | None = None):
        super().__init__(message)
        self.delay = delay


def call_with_retry(fn: Callable[[], T]) -> T:
    """Run ``fn``; on :class:`Retryable` wait (Retry-After, else exponential) and retry up to ``ATTEMPTS`` times.

    The last :class:`Retryable` is re-raised so the caller can record an explicit error instead of a silent zero.
    """
    for attempt in range(ATTEMPTS):
        try:
            return fn()
        except Retryable as e:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(min(MAX_DELAY, e.delay if e.delay is not None else BASE_DELAY * (2 ** attempt)))
    raise AssertionError("unreachable")  # pragma: no cover
