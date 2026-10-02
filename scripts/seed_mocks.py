"""Reset and seed the whole Mock Suite: LedgerLite, MailDesk, and shared files.

Usage: uv run python scripts/seed_mocks.py [--state-dir mocks/state] [--shared shared]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from mocks.ledgerlite.db import TABLES as LEDGERLITE_TABLES
from mocks.maildesk.db import TABLES as MAILDESK_TABLES
from mocks.seed.suite import reset_and_seed_suite

DEFAULT_STATE_DIR = Path("mocks/state")
DEFAULT_SHARED_ROOT = Path("shared")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"directory for the mock SQLite databases (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--shared",
        type=Path,
        default=DEFAULT_SHARED_ROOT,
        help=f"shared file tree root (default: {DEFAULT_SHARED_ROOT})",
    )
    args = parser.parse_args(argv)

    ledgerlite_db = args.state_dir / "ledgerlite.db"
    maildesk_db = args.state_dir / "maildesk.db"
    reset_and_seed_suite(ledgerlite_db, maildesk_db, args.shared)

    print("Seeded Mock Suite")
    print(f"  LedgerLite: {ledgerlite_db}")
    _print_counts(ledgerlite_db, LEDGERLITE_TABLES, indent=4)
    print(f"  MailDesk: {maildesk_db}")
    _print_counts(maildesk_db, MAILDESK_TABLES, indent=4)
    documents = sum(1 for path in args.shared.rglob("*") if path.is_file())
    print(f"  Shared file tree: {args.shared} ({documents} documents)")
    return 0


def _print_counts(db_path: Path, tables: tuple[str, ...], *, indent: int) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{' ' * indent}{table}: {count}")


if __name__ == "__main__":
    sys.exit(main())
