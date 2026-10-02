"""Seed the whole Mock Suite: LedgerLite, MailDesk, and the shared file tree."""

from __future__ import annotations

from pathlib import Path

from mocks.seed.ledgerlite import reset_and_seed as reset_and_seed_ledgerlite
from mocks.seed.maildesk import reset_and_seed as reset_and_seed_maildesk

DEFAULT_LEDGERLITE_DB = Path("mocks/state/ledgerlite.db")
DEFAULT_MAILDESK_DB = Path("mocks/state/maildesk.db")
DEFAULT_SHARED_ROOT = Path("shared")


def reset_and_seed_suite(
    ledgerlite_db: Path = DEFAULT_LEDGERLITE_DB,
    maildesk_db: Path = DEFAULT_MAILDESK_DB,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> None:
    reset_and_seed_ledgerlite(ledgerlite_db)
    reset_and_seed_maildesk(maildesk_db, shared_root)
