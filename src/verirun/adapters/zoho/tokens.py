"""Zoho OAuth tokens: refresh, cache, and encrypt the refresh token at rest.

Zoho Books uses OAuth2. The operator supplies the client id, client secret, and
a refresh token for a read-only scope; the adapter exchanges the refresh token
for short-lived access tokens. The refresh token and the access token are
written to one encrypted file so a restart does not need the operator secret
again, and the file never contains a token in clear text.

The encryption key is an operator secret (``VERIRUN_ZOHO_TOKEN_KEY``). It is
stretched with scrypt and used with Fernet (AES-128-CBC plus HMAC-SHA256).
A missing or wrong key fails with an actionable error, never a token dump.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from verirun.adapters.base import ConflictError, TransientError

TOKEN_KEY_ENV = "VERIRUN_ZOHO_TOKEN_KEY"
TOKEN_URL_PATH = "/oauth/v2/token"
DEFAULT_EXPIRES_IN_S = 3600
DEFAULT_SKEW_S = 60.0
MAX_BACKOFF_S = 8.0
BASE_BACKOFF_S = 0.5
SCRYPT_SALT = b"verirun-zoho-token-v1"


@dataclass(frozen=True)
class ZohoToken:
    """One OAuth token pair and when the access token stops being valid."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scope: str = ""
    api_domain: str = ""

    def usable(self, now: float, skew_seconds: float) -> bool:
        return self.expires_at - skew_seconds > now


@dataclass(frozen=True)
class ZohoOAuthConfig:
    """The operator-supplied OAuth application and refresh token."""

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    accounts_domain: str = "https://accounts.zoho.in"
    timeout: float = 30.0
    max_attempts: int = 3

    @property
    def token_url(self) -> str:
        return f"{self.accounts_domain.rstrip('/')}{TOKEN_URL_PATH}"


class ZohoTokenStore:
    """One encrypted token file, readable only with the operator key."""

    def __init__(self, path: str | Path, key: str) -> None:
        if not key:
            raise ValueError(
                f"a Zoho token file needs an encryption key; set {TOKEN_KEY_ENV}"
            )
        self.path = Path(path)
        self._fernet = _fernet(key)

    def load(self) -> ZohoToken | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except (InvalidToken, ValueError) as exc:
            raise ConflictError(
                f"cannot decrypt the Zoho token file {self.path}; check {TOKEN_KEY_ENV}"
            ) from exc
        if not isinstance(payload, dict):
            raise ConflictError(
                f"the Zoho token file {self.path} is malformed; delete it and reconnect"
            )
        try:
            return ZohoToken(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=float(payload["expires_at"]),
                scope=str(payload.get("scope", "")),
                api_domain=str(payload.get("api_domain", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConflictError(
                f"the Zoho token file {self.path} is malformed; delete it and reconnect"
            ) from exc

    def save(self, token: ZohoToken) -> None:
        payload = json.dumps(
            {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "scope": token.scope,
                "api_domain": token.api_domain,
            }
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".zoho-token-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encrypted)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def clear(self) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()


class ZohoTokenManager:
    """Return a usable access token, refreshing it when it is near expiry."""

    def __init__(
        self,
        config: ZohoOAuthConfig,
        store: ZohoTokenStore,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        skew_seconds: float = DEFAULT_SKEW_S,
    ) -> None:
        self.config = config
        self.store = store
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._skew = skew_seconds

    def access_token(self, *, force: bool = False) -> str:
        cached = self.store.load()
        if cached is not None and not force and cached.usable(self._clock(), self._skew):
            return cached.access_token
        refresh_token = (cached.refresh_token if cached else "") or self.config.refresh_token
        if not refresh_token:
            raise ConflictError(
                "Zoho OAuth has no refresh token; set "
                "VERIRUN_ZOHO_READ_REFRESH_TOKEN or complete the OAuth flow"
            )
        token = self._refresh(refresh_token)
        self.store.save(token)
        return token.access_token

    def _refresh(self, refresh_token: str) -> ZohoToken:
        response = self._post_refresh(refresh_token)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConflictError(
                "Zoho OAuth returned a token response that is not JSON"
            ) from exc
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            error = payload.get("error") or payload.get("message") or "unknown error"
            raise ConflictError(
                f"Zoho OAuth did not return an access token: {error}. "
                "Check VERIRUN_ZOHO_CLIENT_ID, VERIRUN_ZOHO_CLIENT_SECRET, and "
                "VERIRUN_ZOHO_READ_REFRESH_TOKEN."
            )
        try:
            expires_in = int(payload.get("expires_in") or payload.get("expires_in_sec") or DEFAULT_EXPIRES_IN_S)
        except (TypeError, ValueError):
            expires_in = DEFAULT_EXPIRES_IN_S
        return ZohoToken(
            access_token=access_token,
            refresh_token=str(payload.get("refresh_token") or refresh_token),
            expires_at=self._clock() + expires_in,
            scope=str(payload.get("scope", "")),
            api_domain=str(payload.get("api_domain", "")),
        )

    def _post_refresh(self, refresh_token: str) -> httpx.Response:
        http = self._http or httpx.Client(timeout=self.config.timeout)
        data = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": refresh_token,
        }
        last_error = "no attempt was made"
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = http.post(self.config.token_url, data=data)
            except httpx.HTTPError as exc:
                last_error = f"Zoho OAuth token request failed: {type(exc).__name__}"
                if attempt == self.config.max_attempts:
                    raise TransientError(last_error) from exc
                self._sleep(_backoff(attempt))
                continue
            if response.status_code == 200:
                return response
            if response.status_code == 429 or response.status_code >= 500:
                last_error = (
                    f"Zoho OAuth token endpoint returned HTTP {response.status_code}"
                )
                if attempt == self.config.max_attempts:
                    raise TransientError(last_error)
                self._sleep(_retry_after(response) or _backoff(attempt))
                continue
            error = _error_code(response)
            raise ConflictError(
                f"Zoho OAuth rejected the refresh token: {error}. "
                "Check VERIRUN_ZOHO_CLIENT_ID, VERIRUN_ZOHO_CLIENT_SECRET, and "
                "VERIRUN_ZOHO_READ_REFRESH_TOKEN."
            )
        raise TransientError(last_error)


def _fernet(key: str) -> Fernet:
    derived = hashlib.scrypt(
        key.encode("utf-8"), salt=SCRYPT_SALT, n=2**14, r=8, p=1, dklen=32
    )
    return Fernet(base64.urlsafe_b64encode(derived))


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


def _error_code(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("message") or f"HTTP {response.status_code}")
    return f"HTTP {response.status_code}"
