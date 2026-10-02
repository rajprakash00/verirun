from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from company_operator.config import ModelRole, Settings

Message = dict[str, Any]
ToolSpec = dict[str, Any]


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


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model_role: ModelRole = "loop",
    ) -> AssistantTurn: ...


def _request_key(model: str, messages: list[Message], tools: list[ToolSpec] | None) -> str:
    payload = json.dumps(
        {"model": model, "messages": messages, "tools": tools or []},
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
    ) -> AssistantTurn:
        model = self._settings.model_for(model_role)
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return _parse_turn(model, response.json())


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
    ) -> AssistantTurn:
        model = self._settings.model_for(model_role)
        key = _request_key(model, messages, tools)
        path = self._dir / f"{key}.json"
        if self._mode == "replay":
            if not path.exists():
                raise ReplayMissError(f"no fixture for request key {key} in {self._dir}")
            data = json.loads(path.read_text(encoding="utf-8"))
            return AssistantTurn.model_validate(data["response"])
        assert self._inner is not None
        turn = self._inner.complete(messages, tools, model_role)
        path.write_text(
            json.dumps(
                {
                    "request": {"model": model, "messages": messages, "tools": tools or []},
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
