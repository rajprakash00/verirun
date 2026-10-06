"""The uniform Tool interface and its mapping to Observations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from verirun.engine.models import ErrorKind, Observation


class ToolError(Exception):
    """A tool failure that carries the Observation error kind to report.

    ``data`` carries structured detail for the engine's failure policy, for
    example the comparison behind an amount mismatch or the existing record
    behind a duplicate.
    """

    def __init__(
        self, kind: ErrorKind, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.data = data or {}


class Tool(ABC):
    """One function Verirun can call.

    A Tool declares its name, description, and JSON schema, and turns arguments
    into exactly one Observation. Failures never raise: they map to an
    Observation carrying an ``error_kind``. Tools that mutate external state set
    ``side_effect = True`` so the engine journals them and never repeats a
    completed call. Tools whose effects cannot be taken back also set
    ``irreversible = True``, so a policy that requires approval can be prepared
    as an Approval Request instead of being run.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]
    action: ClassVar[str | None] = None
    side_effect: ClassVar[bool] = False
    irreversible: ClassVar[bool] = False

    def invoke(self, args: dict[str, Any] | None = None) -> Observation:
        arguments = dict(args or {})
        try:
            return self.run(arguments)
        except ToolError as exc:
            return Observation(ok=False, summary=exc.message, error_kind=exc.kind, data=exc.data)
        except Exception as exc:  # noqa: BLE001 - tools must never raise into the engine
            return Observation(
                ok=False,
                summary=f"{type(exc).__name__}: {exc}",
                error_kind="unknown",
            )

    @abstractmethod
    def run(self, args: dict[str, Any]) -> Observation:
        """Do the work. Raise ToolError for classified failures."""

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        """Facts the policy gate needs before this tool runs. Read-only."""
        return {}

    def spec(self) -> dict[str, Any]:
        """The OpenAI-compatible function definition for the LLM tool loop."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("invalid", f"missing required argument '{key}'")
    return value


def optional_str(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("invalid", f"argument '{key}' must be a string")
    return value


def optional_bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError("invalid", f"argument '{key}' must be a boolean")
    return value
