import json
from collections.abc import Callable

import httpx
import pytest

from verirun.config import Settings
from verirun.llm.client import LiveClient, LLMRequestError


def test_live_client_returns_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        assert payload["model"] == "loop-model"
        assert payload["tools"][0]["function"]["name"] == "files_list"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "files_list",
                                        "arguments": '{"path": "/tmp"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 60},
                },
            },
        )

    settings = Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
    )
    client = LiveClient(settings, transport=httpx.MockTransport(handler))

    turn = client.complete(
        [{"role": "user", "content": "list files"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "files.list", "description": "list", "parameters": {}},
            }
        ],
    )

    assert turn.tool_calls[0].name == "files.list"
    assert turn.tool_calls[0].arguments == {"path": "/tmp"}
    assert turn.usage.prompt_tokens == 100
    assert turn.usage.cached_tokens == 60


def test_live_client_sends_response_format() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "{}"}}]},
        )

    settings = Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
    )
    client = LiveClient(settings, transport=httpx.MockTransport(handler))

    client.complete(
        [{"role": "user", "content": "reply in json"}],
        response_format={"type": "json_object"},
    )

    assert captured["response_format"] == {"type": "json_object"}


def test_live_client_sanitizes_tool_names_in_messages() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    settings = Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
    )
    client = LiveClient(settings, transport=httpx.MockTransport(handler))

    client.complete(
        [
            {"role": "assistant", "content": "", "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "erp.file_invoice", "arguments": "{}"},
                }
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        ]
    )

    name = captured["messages"][0]["tool_calls"][0]["function"]["name"]
    assert name == "erp_file_invoice"


def test_live_client_reports_the_provider_error_body() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid 'tools[0].function.name'"}},
        )

    settings = Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
    )
    client = LiveClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(LLMRequestError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])

    assert "400" in str(excinfo.value)
    assert "Invalid 'tools[0].function.name'" in str(excinfo.value)
    assert calls == 1


def _retrying_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> LiveClient:
    monkeypatch.setattr("verirun.llm.client.time.sleep", lambda _seconds: None)
    settings = Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
    )
    return LiveClient(settings, transport=httpx.MockTransport(handler))


def test_live_client_retries_a_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    client = _retrying_client(monkeypatch, handler)

    turn = client.complete([{"role": "user", "content": "hi"}])

    assert turn.text == "ok"
    assert calls == 2


def test_live_client_retries_a_busy_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "try later"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    client = _retrying_client(monkeypatch, handler)

    turn = client.complete([{"role": "user", "content": "hi"}])

    assert turn.text == "ok"
    assert calls == 2


def test_live_client_gives_up_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _retrying_client(monkeypatch, handler)

    with pytest.raises(httpx.ReadTimeout):
        client.complete([{"role": "user", "content": "hi"}])

    assert calls == 3
