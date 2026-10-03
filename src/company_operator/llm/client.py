from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from company_operator.config import ModelRole, Settings

Message = dict[str, Any]
ToolSpec = dict[str, Any]

# OpenAI-compatible endpoints reject function names outside this pattern, so
# dotted engine names (mail.list) are translated at the wire boundary.
WIRE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")

# Transport errors and busy gateways are retried; a bad request is not.
REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AssistantTurn(BaseModel):
    model: str
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


class ReplayMissError(RuntimeError):
    """Raised when replay mode finds no fixture for a request."""


class LLMRequestError(RuntimeError):
    """A non-2xx reply from the provider, carrying its error body."""


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model_role: ModelRole = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn: ...


def _request_key(
    model: str,
    messages: list[Message],
    tools: list[ToolSpec] | None,
    response_format: dict[str, Any] | None = None,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": tools or [],
            "response_format": response_format,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:24]


def _parse_turn(model: str, data: dict[str, Any]) -> AssistantTurn:
    choice = (data.get("choices") or [{}])[0].get("message") or {}
    tool_calls: list[ToolCall] = []
    for raw_call in choice.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            arguments = {"_raw": raw_args}
        tool_calls.append(
            ToolCall(id=raw_call.get("id", ""), name=function.get("name", ""), arguments=arguments)
        )
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0
    return AssistantTurn(
        model=model,
        text=choice.get("content"),
        tool_calls=tool_calls,
        usage=Usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cached_tokens=cached,
        ),
    )


def _wire_name(name: str) -> str:
    """A function name the provider accepts, derived from an engine tool name."""
    return WIRE_NAME_PATTERN.sub("_", name)


def _wire_tools(tools: list[ToolSpec]) -> tuple[list[ToolSpec], dict[str, str]]:
    """Copy tool specs with provider-safe names, plus the mapping back."""
    wire: list[ToolSpec] = []
    originals: dict[str, str] = {}
    for spec in tools:
        function = spec.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            safe = _wire_name(name)
            if safe != name:
                originals.setdefault(safe, name)
                spec = {**spec, "function": {**function, "name": safe}}
        wire.append(spec)
    return wire, originals


def _wire_message(message: Message) -> Message:
    """Copy a message, renaming any tool call to its provider-safe name."""
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return message
    wire_calls = []
    for call in tool_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            safe = _wire_name(name)
            if safe != name:
                call = {**call, "function": {**function, "name": safe}}
        wire_calls.append(call)
    return {**message, "tool_calls": wire_calls}


def _provider_error(response: httpx.Response) -> LLMRequestError:
    detail = response.text.strip()
    if len(detail) > 500:
        detail = detail[:500] + "..."
    return LLMRequestError(
        f"{response.status_code} from {response.request.url}: {detail}"
    )


class LiveClient:
    """Chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        settings: Settings,
        session_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "User-Agent": "operator/0.1",
        }
        if session_id:
            headers["x-opencode-session"] = session_id
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            timeout=settings.request_timeout_s,
            transport=transport,
        )

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model_role: ModelRole = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        model = self._settings.model_for(model_role)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_wire_message(message) for message in messages],
        }
        originals: dict[str, str] = {}
        if tools:
            payload["tools"], originals = _wire_tools(tools)
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        response = self._post(payload)
        if response.is_error:
            raise _provider_error(response)
        turn = _parse_turn(model, response.json())
        for call in turn.tool_calls:
            call.name = originals.get(call.name, call.name)
        return turn

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST with bounded retries for transient transport and gateway failures."""
        for attempt in range(REQUEST_ATTEMPTS):
            last = attempt == REQUEST_ATTEMPTS - 1
            try:
                response = self._client.post("/chat/completions", json=payload)
            except httpx.TransportError:
                if last:
                    raise
            else:
                if response.status_code not in RETRYABLE_STATUS or last:
                    return response
            time.sleep(RETRY_BACKOFF_S * (2**attempt))
        raise AssertionError("unreachable: the retry loop always returns or raises")


class ReplayClient:
    """Serves recorded fixtures in replay mode, or records live calls in record mode."""

    def __init__(
        self,
        settings: Settings,
        mode: Literal["record", "replay"] = "replay",
        inner: LLMClient | None = None,
    ) -> None:
        if mode == "record" and inner is None:
            inner = LiveClient(settings)
        self._settings = settings
        self._mode = mode
        self._inner = inner
        self._dir = settings.fixture_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model_role: ModelRole = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        model = self._settings.model_for(model_role)
        key = _request_key(model, messages, tools, response_format)
        path = self._dir / f"{key}.json"
        if self._mode == "replay":
            if not path.exists():
                raise ReplayMissError(f"no fixture for request key {key} in {self._dir}")
            data = json.loads(path.read_text(encoding="utf-8"))
            return AssistantTurn.model_validate(data["response"])
        assert self._inner is not None
        turn = self._inner.complete(messages, tools, model_role, response_format)
        path.write_text(
            json.dumps(
                {
                    "request": {
                        "model": model,
                        "messages": messages,
                        "tools": tools or [],
                        "response_format": response_format,
                    },
                    "response": turn.model_dump(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return turn


def build_client(settings: Settings, session_id: str | None = None) -> LLMClient:
    if settings.llm_mode == "replay":
        return ReplayClient(settings, "replay")
    if settings.llm_mode == "record":
        return ReplayClient(settings, "record", LiveClient(settings, session_id=session_id))
    return LiveClient(settings, session_id=session_id)
