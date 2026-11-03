"""Store errors shared with the engine without importing the store module."""

from __future__ import annotations

from typing import Any


class UnknownActionError(RuntimeError):
    """A journal entry has an unknown outcome and must not be invoked again."""


class ClaimConflictError(RuntimeError):
    """Another Run already holds the natural key this action would write."""

    def __init__(self, claim: Any) -> None:
        super().__init__(
            f"{claim.claim_key} is already claimed by Run {claim.run_id} "
            f"({claim.journal_key})"
        )
        self.claim = claim
