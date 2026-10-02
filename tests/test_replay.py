import json

import httpx
import pytest

from company_operator.config import Settings
from company_operator.llm.client import LiveClient, ReplayClient, ReplayMissError


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="test-key",
        base_url="https://example.test/v1",
        model_loop="loop-model",
        fixture_dir=tmp_path,
    )


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "recorded answer",
                        "tool_calls": [],
                    }
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
    )


def test_record_then_replay_returns_the_same_turn(tmp_path) -> None:
    settings = _settings(tmp_path)
    live = LiveClient(settings, transport=httpx.MockTransport(_handler))
    recorder = ReplayClient(settings, mode="record", inner=live)
    messages = [{"role": "user", "content": "hello"}]

    recorded = recorder.complete(messages)
    replayed = ReplayClient(settings, mode="replay").complete(messages)

    assert replayed == recorded
    assert replayed.text == "recorded answer"


def test_replay_miss_raises_a_clear_error(tmp_path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ReplayMissError):
        ReplayClient(settings, mode="replay").complete([{"role": "user", "content": "unknown"}])


def test_record_mode_writes_a_fixture_file(tmp_path) -> None:
    settings = _settings(tmp_path)
    live = LiveClient(settings, transport=httpx.MockTransport(_handler))
    ReplayClient(settings, mode="record", inner=live).complete([{"role": "user", "content": "hi"}])

    fixtures = list(tmp_path.glob("*.json"))
    assert len(fixtures) == 1
    payload = json.loads(fixtures[0].read_text())
    assert payload["response"]["text"] == "recorded answer"
