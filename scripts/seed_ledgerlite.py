"""Reset LedgerLite's SQLite database and load the seeded scenarios.

Usage: uv run python scripts/seed_ledgerlite.py [--db mocks/state/ledgerlite.db]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mocks.ledgerlite.db import TABLES, connect
from mocks.seed.ledgerlite import reset_and_seed

DEFAULT_DB = Path("mocks/state/ledgerlite.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database to reset and seed (default: {DEFAULT_DB})",
    )
    args = parser.parse_args(argv)

    reset_and_seed(args.db)
    conn = connect(args.db)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
    finally:
        conn.close()

    print(f"Seeded {args.db}")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
