"""OAuth tokens for the Zoho Books adapter: refresh, reuse, and encryption at rest."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from verirun.adapters import ConflictError, TransientError
from verirun.adapters.zoho.tokens import (
    ZohoOAuthConfig,
    ZohoToken,
    ZohoTokenManager,
    ZohoTokenStore,
)

TOKEN_RESPONSE = {
    "access_token": "access-token-1",
    "refresh_token": "refresh-token-1",
    "expires_in": 3600,
    "scope": "ZohoBooks.contacts.READ,ZohoBooks.purchaseorders.READ,ZohoBooks.bills.READ,ZohoBooks.vendorpayments.READ",
    "api_domain": "https://www.zohoapis.in",
    "token_type": "Bearer",
}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def token_client(handler, calls: list[httpx.Request]) -> httpx.Client:
    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(record))


def manager(
    tmp_path: Path,
    handler,
    calls: list[httpx.Request],
    *,
    clock: Clock | None = None,
    read_only: bool = True,
) -> ZohoTokenManager:
    config = ZohoOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token-1",
        accounts_domain="https://accounts.zoho.in",
        read_only=read_only,
    )
    return ZohoTokenManager(
        config,
        ZohoTokenStore(tmp_path / "zoho-token.enc", "operator-secret"),
        http=token_client(handler, calls),
        clock=clock or Clock(),
        sleep=lambda seconds: None,
    )


def test_a_refreshed_token_is_encrypted_at_rest_and_reused(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://accounts.zoho.in/oauth/v2/token"
        assert request.method == "POST"
        return httpx.Response(200, json=TOKEN_RESPONSE)

    oauth = manager(tmp_path, handler, calls)

    first = oauth.access_token()
    second = oauth.access_token()

    assert first == second == "access-token-1"
    assert len(calls) == 1
    token_file = tmp_path / "zoho-token.enc"
    raw = token_file.read_bytes()
    assert b"access-token-1" not in raw
    assert b"refresh-token-1" not in raw
    assert token_file.stat().st_mode & 0o777 == 0o600
    stored = ZohoTokenStore(token_file, "operator-secret").load()
    assert stored is not None
    assert stored.access_token == "access-token-1"
    assert stored.refresh_token == "refresh-token-1"


def test_an_expired_token_is_refreshed_and_the_rotated_refresh_token_is_kept(
    tmp_path: Path,
) -> None:
    clock = Clock()
    calls: list[httpx.Request] = []
    responses = iter(
        [
            {**TOKEN_RESPONSE, "access_token": "access-token-1", "refresh_token": "rotated"},
            {**TOKEN_RESPONSE, "access_token": "access-token-2"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    oauth = manager(tmp_path, handler, calls, clock=clock)
    assert oauth.access_token() == "access-token-1"

    clock.now += 3600
    assert oauth.access_token() == "access-token-2"
    assert len(calls) == 2
    bodies = [
        dict(pair.split("=") for pair in call.content.decode().split("&"))
        for call in calls
    ]
    assert bodies[0]["refresh_token"] == "refresh-token-1"
    assert bodies[1]["refresh_token"] == "rotated"


def test_a_token_is_refreshed_before_it_expires(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**TOKEN_RESPONSE, "expires_in": 180})

    oauth = manager(tmp_path, handler, calls, clock=clock)
    assert oauth.access_token() == "access-token-1"

    clock.now += 60
    assert oauth.access_token() == "access-token-1"
    assert len(calls) == 1

    clock.now += 60
    assert oauth.access_token() == "access-token-1"
    assert len(calls) == 2


def test_a_token_endpoint_rate_limit_is_retried(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate_limit"})
        return httpx.Response(200, json=TOKEN_RESPONSE)

    oauth = manager(tmp_path, handler, calls)

    assert oauth.access_token() == "access-token-1"
    assert attempts["count"] == 2


def test_a_rejected_refresh_token_is_an_actionable_conflict(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    oauth = manager(tmp_path, handler, [])

    with pytest.raises(ConflictError) as excinfo:
        oauth.access_token()

    assert "invalid_client" in str(excinfo.value)
    assert "VERIRUN_ZOHO" in str(excinfo.value)


def test_exhausted_token_endpoint_transient_failures_are_transient(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    oauth = manager(tmp_path, handler, [])

    with pytest.raises(TransientError):
        oauth.access_token()


def test_a_write_scoped_token_is_rejected_by_the_read_only_client(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={**TOKEN_RESPONSE, "scope": "ZohoBooks.bills.CREATE"},
        )

    oauth = manager(tmp_path, handler, [])

    with pytest.raises(ConflictError, match="read-only"):
        oauth.access_token()


def test_a_cached_write_scoped_token_is_rejected(tmp_path: Path) -> None:
    store = ZohoTokenStore(tmp_path / "zoho-token.enc", "operator-secret")
    store.save(
        ZohoToken(
            access_token="access-token-1",
            refresh_token="refresh-token-1",
            expires_at=2_000_000.0,
            scope="ZohoBooks.bills.ALL",
        )
    )
    oauth = manager(tmp_path, lambda request: httpx.Response(200), [])

    with pytest.raises(ConflictError, match="read-only"):
        oauth.access_token()


def test_a_token_file_needs_the_operator_key(tmp_path: Path) -> None:
    store = ZohoTokenStore(tmp_path / "zoho-token.enc", "one-key")
    store.save(
        ZohoToken(
            access_token="access-token-1",
            refresh_token="refresh-token-1",
            expires_at=2_000_000.0,
        )
    )

    with pytest.raises(ConflictError):
        ZohoTokenStore(tmp_path / "zoho-token.enc", "another-key").load()


def test_a_token_store_refuses_an_empty_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="VERIRUN_ZOHO_TOKEN_KEY"):
        ZohoTokenStore(tmp_path / "zoho-token.enc", "")


def test_a_missing_token_store_can_be_bootstrapped_from_the_environment(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=TOKEN_RESPONSE)

    oauth = manager(tmp_path, handler, calls)
    assert not (tmp_path / "zoho-token.enc").exists()

    assert oauth.access_token() == "access-token-1"
    assert (tmp_path / "zoho-token.enc").is_file()
    stored = ZohoTokenStore(tmp_path / "zoho-token.enc", "operator-secret").load()
    assert stored is not None
    assert stored.refresh_token == "refresh-token-1"
