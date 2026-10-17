"""Record the eval harness's replay fixtures from the scripted scenarios.

Usage: uv run python scripts/record_eval_fixtures.py

Clears evals/fixtures/llm, then runs every seeded scenario with a recording
ReplayClient whose inner client is the scenario's own script. The scripts are
deterministic, so no .env file and no live model are needed. Exits non-zero
when the recorded run does not pass its own expectations.
"""

from __future__ import annotations

import sys
from pathlib import Path

from verirun.config import Settings
from verirun.eval import format_report, run_eval
from verirun.llm.client import ReplayClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "evals" / "fixtures" / "llm"


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    cleared = 0
    for fixture in FIXTURE_DIR.glob("*.json"):
        fixture.unlink()
        cleared += 1
    print(f"Cleared {cleared} fixture(s) from {FIXTURE_DIR.relative_to(ROOT)}")

    settings = Settings(
        _env_file=None,
        llm_mode="record",
        fixture_dir=FIXTURE_DIR,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=ROOT / "runs" / "eval" / "verirun.db",
        shared_dir=ROOT / "shared",
        mail_db=ROOT / "mocks" / "state" / "maildesk.db",
        erp_db=ROOT / "mocks" / "state" / "ledgerlite.db",
    )
    report = run_eval(
        settings,
        client_factory=lambda settings, scenario: ReplayClient(
            settings, mode="record", inner=scenario.script()
        ),
    )
    print(format_report(report))
    recorded = len(list(FIXTURE_DIR.glob("*.json")))
    print(f"Recorded {recorded} fixture(s) into {FIXTURE_DIR.relative_to(ROOT)}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
