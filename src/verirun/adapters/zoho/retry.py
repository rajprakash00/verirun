"""Retry policy shared by the Zoho token and Books clients."""

from __future__ import annotations

import httpx

BASE_BACKOFF_S = 0.5
MAX_BACKOFF_S = 8.0
MAX_RETRY_AFTER_S = 60.0


def backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** (attempt - 1)))


def retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        return None
