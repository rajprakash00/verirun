"""Record live LLM fixtures for the Resolve and Plan acceptance paths.

Usage: uv run python scripts/record_llm_fixtures.py

Reads the gateway settings from .env. Writes one fixture per LLM call into
OPERATOR_FIXTURE_DIR (default tests/fixtures/llm). Re-run whenever the company
context, the Task Pack, or the prompts change.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from company_operator.config import Settings
from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.orchestrator import start_run
from company_operator.llm.client import build_client
from company_operator.runs.store import RunStore

REQUESTS = (
    ("Process the invoices in the AP mailbox", "invoice-processing"),
    ("Handle whatever is going on with the vendor", "invoice-processing"),
)


def main() -> int:
    settings = Settings(llm_mode="record")
    defaults = Settings(_env_file=None)
    for role, model in (
        ("loop", settings.model_loop),
        ("reason", settings.model_reason),
        ("vision", settings.model_vision),
    ):
        if model != defaults.model_for(role):
            print(
                f"warning: {role} model is {model!r}, but the replay tests use "
                f"{defaults.model_for(role)!r}; fixtures will not replay",
                file=sys.stderr,
            )

    context = load_company_context(settings.company_dir)
    print(f"Recording fixtures into {settings.fixture_dir}")
    for index, (request, task_id) in enumerate(REQUESTS, start=1):
        task_pack = load_task_pack(settings.tasks_dir / f"{task_id}.yaml")
        run_id = f"RECORD-{index:02d}"
        client = build_client(settings, session_id=run_id)
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "record.db")
            run = start_run(request, task_pack, context, client, store, run_id=run_id)
        plan_steps = len(run.plan.steps) if run.plan else 0
        print(
            f"  {run_id}: {request!r} -> {run.state.value} "
            f"(policies={run.work_order.policies}, steps={plan_steps}, "
            f"questions={len(run.work_order.open_questions)})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
