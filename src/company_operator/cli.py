from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operator",
        description="Turn a company request into completed work.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start a Run from a Request")
    run.add_argument("request", help="the short request, for example: process the invoices")
    run.add_argument("--task", default=None, help="Task Pack id; inferred from the request when omitted")

    subparsers.add_parser("serve", help="serve the local dashboard")

    report = subparsers.add_parser("report", help="render the Evidence Pack for a Run")
    report.add_argument("run_id", help="Run id")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        print("run is not implemented yet", file=sys.stderr)
        return 1
    if args.command == "serve":
        print("serve is not implemented yet", file=sys.stderr)
        return 1
    if args.command == "report":
        print("report is not implemented yet", file=sys.stderr)
        return 1
    return 0
