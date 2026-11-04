"""The uniform Tool interface and its mapping to Observations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

from verirun.adapters import AdapterError
from verirun.engine.models import ErrorKind, Observation

T = TypeVar("T")


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

    def claim_key(self, args: dict[str, Any]) -> str | None:
        """The natural key this side effect must not write twice.

        A bill, for example, is unique by vendor and invoice number. The engine
        commits the claim with the journal intent, so a second Run cannot write
        the same natural key while a crash or an unknown outcome is unresolved.
        """
        return None

    def claim_detail(self, args: dict[str, Any]) -> dict[str, Any]:
        """The facts a human needs when this natural key is already claimed."""
        return {}

    def reconcile(
        self, args: dict[str, Any], idempotency_key: str
    ) -> Observation | None:
        """Look for this side effect's earlier write after a crash.

        Return a successful Observation when the effect already landed, or
        ``None`` when it provably did not. Raise :class:`ToolError` with an
        ``unknown`` kind when the system cannot say; the engine then parks the
        Run instead of retrying the write.
        """
        return None

    def adapter_call(self, method: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call an adapter and map its classified failure to a ToolError."""
        try:
            return method(*args, **kwargs)
        except AdapterError as exc:
            raise ToolError(exc.kind, exc.message, exc.data) from exc

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
