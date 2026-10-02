import json

import httpx

from company_operator.config import Settings
from company_operator.llm.client import LiveClient


def test_live_client_returns_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        assert payload["model"] == "loop-model"
        assert payload["tools"][0]["function"]["name"] == "files.list"
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
                                        "name": "files.list",
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
