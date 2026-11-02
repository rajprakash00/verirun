"""The Zoho Books HTTP client: auth, pagination, rate limits, and backoff.

Zoho Books answers a list with a ``page_context``; a client that reads only the
first page silently misses vendors, purchase orders, or bills. Zoho also rate
limits per organisation and occasionally returns 5xx. This client walks every
page, retries a rate-limited or transient request, honours ``Retry-After``, and
refreshes an expired access token once before failing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from verirun.adapters.base import ConflictError, NotFoundError, TransientError
from verirun.adapters.zoho.tokens import ZohoTokenManager

DEFAULT_BASE_URL = "https://www.zohoapis.in/books/v3"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PER_PAGE = 200
MAX_PAGES = 1_000
MAX_BACKOFF_S = 8.0
BASE_BACKOFF_S = 0.5
NOT_FOUND_CODES = frozenset({1002})


class ZohoBooksClient:
    """A small typed wrapper over the Zoho Books v3 API."""

    def __init__(
        self,
        *,
        token_manager: ZohoTokenManager,
        organization_id: str,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not organization_id:
            raise ValueError("a Zoho Books client needs an organization id")
        self.token_manager = token_manager
        self.organization_id = organization_id
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self.max_attempts = max_attempts
        self._sleep = sleep

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GET, with retries and a single token refresh on 401."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {"organization_id": self.organization_id, **(params or {})}
        refreshed = False
        forced_token: str | None = None
        last_error = "no attempt was made"
        for attempt in range(1, self.max_attempts + 1):
            if forced_token is not None:
                token = forced_token
                forced_token = None
            else:
                token = self.token_manager.access_token()
            headers = {
                "Authorization": f"Zoho-oauthtoken {token}",
                "Accept": "application/json",
            }
            try:
                response = self._http.get(url, params=query, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"Zoho Books request failed: {type(exc).__name__}"
                if attempt == self.max_attempts:
                    raise TransientError(last_error) from exc
                self._sleep(_backoff(attempt))
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = True
                forced_token = self.token_manager.access_token(force=True)
                continue
            if response.status_code == 401:
                raise ConflictError(
                    "Zoho Books rejected the access token after a refresh; "
                    "reconnect the Zoho OAuth application"
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"Zoho Books returned HTTP {response.status_code} for {path}"
                if attempt == self.max_attempts:
                    raise TransientError(last_error)
                self._sleep(_retry_after(response) or _backoff(attempt))
                continue
            if response.status_code == 404:
                raise NotFoundError(_error_message(response, path))
            if response.status_code >= 400:
                raise ConflictError(_error_message(response, path))
            return self._payload(response, path)
        raise TransientError(last_error)

    def get_all(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Every record of one list endpoint, following ``page_context``."""
        items: list[dict[str, Any]] = []
        base = {"per_page": DEFAULT_PER_PAGE, **(params or {})}
        for number in range(1, MAX_PAGES + 1):
            payload = self.get(path, {**base, "page": number})
            page_items = payload.get(key) or []
            if not isinstance(page_items, list):
                raise ConflictError(f"Zoho Books returned a non-list '{key}' for {path}")
            items.extend(item for item in page_items if isinstance(item, dict))
            context = payload.get("page_context") or {}
            if not context.get("has_more_page"):
                return items
        raise ConflictError(f"Zoho Books pagination for {path} exceeded {MAX_PAGES} pages")

    def _payload(self, response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConflictError(f"Zoho Books returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise ConflictError(f"Zoho Books returned an unexpected payload for {path}")
        code = payload.get("code", 0)
        if code:
            message = str(payload.get("message") or f"error code {code}")
            if code in NOT_FOUND_CODES or "not found" in message.casefold():
                raise NotFoundError(message, data={"zoho_code": code, "path": path})
            raise ConflictError(message, data={"zoho_code": code, "path": path})
        return payload


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** (attempt - 1)))


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(MAX_BACKOFF_S, max(0.0, float(raw)))
    except ValueError:
        return None


def _error_message(response: httpx.Response, path: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Zoho Books returned HTTP {response.status_code} for {path}"
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)
    return f"Zoho Books returned HTTP {response.status_code} for {path}"
