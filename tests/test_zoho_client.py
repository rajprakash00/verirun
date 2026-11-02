"""The Zoho Books HTTP client: pagination, retries, auth, and error mapping."""

from __future__ import annotations

import httpx
import pytest

from verirun.adapters import ConflictError, NotFoundError, TransientError
from verirun.adapters.zoho.client import ZohoBooksClient


class FakeTokens:
    def __init__(self) -> None:
        self.requests = 0
        self.forced = 0

    def access_token(self, *, force: bool = False) -> str:
        if force:
            self.forced += 1
            return "fresh-token"
        self.requests += 1
        return "access-token"


def client(handler, *, sleep=None, tokens=None, max_attempts=3) -> ZohoBooksClient:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    result = ZohoBooksClient(
        token_manager=tokens or FakeTokens(),
        organization_id="6000000000001",
        base_url="https://www.zohoapis.in/books/v3",
        http=httpx.Client(transport=httpx.MockTransport(record)),
        sleep=sleep or (lambda seconds: None),
        max_attempts=max_attempts,
    )
    result.calls = calls  # type: ignore[attr-defined]
    return result


def page(items: list[dict], number: int, *, more: bool) -> dict:
    return {
        "code": 0,
        "message": "success",
        "page_context": {
            "page": number,
            "per_page": 200,
            "has_more_page": more,
            "report_name": "Vendors",
        },
        "vendors": items,
    }


def test_pagination_follows_page_context_until_the_last_page() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        number = int(request.url.params["page"])
        if number == 1:
            return httpx.Response(200, json=page([{"id": "1"}, {"id": "2"}], 1, more=True))
        return httpx.Response(200, json=page([{"id": "3"}], 2, more=False))

    api = client(handler)

    items = api.get_all("/contacts", "vendors")

    assert [item["id"] for item in items] == ["1", "2", "3"]
    assert [entry["page"] for entry in seen] == ["1", "2"]
    assert all(entry["organization_id"] == "6000000000001" for entry in seen)
    assert api.calls[0].headers["Authorization"] == "Zoho-oauthtoken access-token"


def test_a_rate_limited_request_is_retried_after_retry_after() -> None:
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"code": 429})
        return httpx.Response(200, json={"code": 0, "contact": {"contact_id": "1"}})

    api = client(handler, sleep=sleeps.append)

    assert api.get("/contacts/1")["contact"]["contact_id"] == "1"
    assert sleeps == [3.0]
    assert attempts["count"] == 2


def test_server_errors_are_retried_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={"code": 503})
        return httpx.Response(200, json={"code": 0, "contact": {"contact_id": "1"}})

    api = client(handler, sleep=sleeps.append)

    assert api.get("/contacts/1")["contact"]["contact_id"] == "1"
    assert sleeps == [0.5, 1.0]


def test_exhausted_transient_failures_raise_a_transient_error() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503})

    api = client(handler, sleep=sleeps.append)

    with pytest.raises(TransientError):
        api.get("/contacts/1")
    assert len(sleeps) == 2


def test_a_missing_record_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 1002, "message": "Record not found"})

    api = client(handler)

    with pytest.raises(NotFoundError):
        api.get("/contacts/999")


def test_a_zoho_error_envelope_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 1002, "message": "The record does not exist"}
        )

    api = client(handler)

    with pytest.raises(NotFoundError):
        api.get("/contacts/999")


def test_an_expired_access_token_is_refreshed_once() -> None:
    tokens = FakeTokens()
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(401, json={"code": 401})
        assert request.headers["Authorization"] == "Zoho-oauthtoken fresh-token"
        return httpx.Response(200, json={"code": 0, "contact": {"contact_id": "1"}})

    api = client(handler, tokens=tokens)

    assert api.get("/contacts/1")["contact"]["contact_id"] == "1"
    assert tokens.forced == 1


def test_a_second_unauthorized_response_is_a_conflict() -> None:
    tokens = FakeTokens()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "invalid token"})

    api = client(handler, tokens=tokens)

    with pytest.raises(ConflictError, match="access token"):
        api.get("/contacts/1")
    assert tokens.forced == 1


def test_an_invalid_request_is_a_conflict_with_the_zoho_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"code": 4000, "message": "Invalid value for parameter"}
        )

    api = client(handler)

    with pytest.raises(ConflictError, match="Invalid value"):
        api.get("/contacts", {"contact_type": "wrong"})
