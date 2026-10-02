"""Asking the model for JSON and refusing to accept anything else."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from company_operator.config import ModelRole
from company_operator.llm.client import LLMClient, Message
from company_operator.validation import format_validation_errors

FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredOutputError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    text = text.strip()
    match = FENCED_JSON.search(text)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def complete_structured[ModelT: BaseModel](
    client: LLMClient,
    messages: list[Message],
    schema: type[ModelT],
    *,
    model_role: ModelRole = "reason",
    repair_attempts: int = 1,
    validate: Callable[[ModelT], str | None] | None = None,
) -> ModelT:
    conversation = [dict(message) for message in messages]
    last_problem = "the model returned an empty reply"
    for attempt in range(repair_attempts + 1):
        turn = client.complete(
            conversation,
            model_role=model_role,
            response_format={"type": "json_object"},
        )
        text = (turn.text or "").strip()
        try:
            model = schema.model_validate(extract_json(text))
            if validate is not None:
                problem = validate(model)
                if problem:
                    raise ValueError(problem)
            return model
        except ValueError as exc:
            last_problem = _describe(exc)
            if attempt == repair_attempts:
                break
            conversation.append({"role": "assistant", "content": text or "(empty reply)"})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"That reply was rejected: {last_problem}. "
                        "Reply again with only the corrected JSON object."
                    ),
                }
            )
    raise StructuredOutputError(
        f"the model did not produce valid structured output: {last_problem}"
    )


def _describe(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        return format_validation_errors(exc)
    return str(exc)
