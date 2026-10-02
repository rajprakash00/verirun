"""Serve MailDesk locally, seeding mailbox and shared files first unless --no-seed.

Usage: uv run python scripts/serve_maildesk.py [--db mocks/state/maildesk.db] [--shared shared]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from mocks.maildesk.app import create_app
from mocks.seed.maildesk import reset_and_seed

DEFAULT_DB = Path("mocks/state/maildesk.db")
DEFAULT_SHARED_ROOT = Path("shared")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database to serve (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--shared",
        type=Path,
        default=DEFAULT_SHARED_ROOT,
        help=f"shared file tree root (default: {DEFAULT_SHARED_ROOT})",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="serve the existing mailbox instead of resetting and seeding it",
    )
    args = parser.parse_args(argv)

    if not args.no_seed:
        reset_and_seed(args.db, args.shared)
    uvicorn.run(create_app(args.db, args.shared), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
