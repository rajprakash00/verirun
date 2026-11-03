"""The PostgreSQL run store and worker queue.

Skipped unless ``VERIRUN_TEST_DATABASE_URL`` points at a disposable database.
CI provides one; locally use any empty PostgreSQL database:

    VERIRUN_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/verirun_test \\
        uv run pytest tests/test_postgres_store.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support import ROOT, ScriptedClient, text_turn
from verirun.adapters import InMemoryAccountingSystem, MockMailSource
from verirun.config import Settings
from verirun.engine.models import Observation
from verirun.engine.states import RunState
from verirun.runs.store import ClaimConflictError, RunStore, UnknownActionError
from verirun.runtime import Backends
from verirun.worker import Worker

DSN = os.environ.get("VERIRUN_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set VERIRUN_TEST_DATABASE_URL to run PostgreSQL tests"
)


@pytest.fixture
def store() -> RunStore:
    import psycopg

    with psycopg.connect(DSN) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return RunStore(DSN)


def test_runs_and_journal_round_trip_on_postgres(store: RunStore) -> None:
    assert store.kind == "postgres"
    run = store.create_run("Process the invoices", "invoice-processing", run_id="RUN-PG-1")
    store.transition(run.id, RunState.RESOLVING)
    store.add_observation(
        run.id, Observation(ok=True, summary="listed"), tool="mail.list"
    )

    assert store.get_run(run.id).state is RunState.RESOLVING
    assert store.list_observations(run.id)[0].summary == "listed"
    assert store.record_action(run.id, "k", "erp.file_invoice", {"n": 1})
    store.complete_action(run.id, "k", {"ok": True})
    entry = store.get_journal_entry(run.id, "k")
    assert entry is not None and entry.status == "done"


def test_unknown_journal_and_exception_work_on_postgres(store: RunStore) -> None:
    run = store.create_run("Process the invoices", "invoice-processing", run_id="RUN-PG-2")
    store.record_action(run.id, "k", "erp.file_invoice", {"n": 1})
    store.mark_action_unknown(run.id, "k", "timeout")

    with pytest.raises(UnknownActionError):
        store.record_action(run.id, "k", "erp.file_invoice", {"n": 1})
    exception = store.record_exception(
        run.id,
        kind="unknown_write_outcome",
        summary="timeout",
        question="Reconcile the bill",
        evidence={"claim_key": "bill:V-1:INV-9"},
        journal_key="k",
    )
    assert store.open_exception(run.id) == exception
    assert store.resolve_exception(exception.id, resolution="bill exists").status == "resolved"


def test_natural_key_claims_are_unique_on_postgres(store: RunStore) -> None:
    first = store.create_run("one", "invoice-processing", run_id="RUN-PG-3")
    second = store.create_run("two", "invoice-processing", run_id="RUN-PG-4")
    store.record_action(
        first.id,
        "k1",
        "erp.file_invoice",
        {"n": 1},
        claim_key="bill:V-1:INV-9",
    )
    with pytest.raises(ClaimConflictError):
        store.record_action(
            second.id,
            "k2",
            "erp.file_invoice",
            {"n": 1},
            claim_key="bill:V-1:INV-9",
        )


def test_postgres_queue_leases_with_skip_locked(store: RunStore) -> None:
    run = store.create_run("one", "invoice-processing", run_id="RUN-PG-5")
    start = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    store.enqueue(run.id, "start", now=start)

    first = store.claim("worker-a", lease_seconds=10, now=start)
    second = store.claim("worker-b", lease_seconds=10, now=start)
    assert first is not None and first.status == "leased"
    assert second is None

    reclaimed = store.claim(
        "worker-b", lease_seconds=10, now=start + timedelta(seconds=11)
    )
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.attempts == 2
    assert reclaimed.lease_owner == "worker-b"


def test_two_postgres_workers_claim_different_jobs(store: RunStore) -> None:
    from concurrent.futures import ThreadPoolExecutor

    first = store.create_run("one", "invoice-processing", run_id="RUN-PG-7")
    second = store.create_run("two", "invoice-processing", run_id="RUN-PG-8")
    store.enqueue(first.id, "start")
    store.enqueue(second.id, "start")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda worker: store.claim(worker),
                ["worker-a", "worker-b"],
            )
        )

    assert all(claim is not None for claim in claims)
    assert {claim.run_id for claim in claims if claim} == {first.id, second.id}


def test_the_worker_runs_a_queued_run_on_the_postgres_store(
    store: RunStore, tmp_path: Path
) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "pg-smoke.yaml").write_text(
        """\
id: pg-smoke
title: PostgreSQL smoke
goal_template: >-
  Finish without tools. Request: {request}
sop: invoice-processing
tools:
  - files.list
verification: []
""",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=tasks,
        run_db=tmp_path / "unused.db",
        database_url=DSN,
        shared_dir=tmp_path / "shared",
    )
    store.create_run("Finish", "pg-smoke", run_id="RUN-PG-6")
    store.enqueue("RUN-PG-6", "start")

    # The start job resolves and plans through the scripted client, then the
    # empty verification contract completes the Run.
    work_order = {
        "sop": "invoice-processing",
        "goal": "Finish",
        "assumptions": [],
        "systems": ["files"],
        "policies": [],
        "approval_gates": [],
        "success_criteria": ["The Run completed"],
        "open_questions": [],
    }
    plan = {
        "steps": [
            {
                "id": "step-1",
                "goal": "List the files",
                "allowed_tools": ["files.list"],
                "done_criterion": "The directory was read",
            }
        ]
    }
    client = ScriptedClient(
        [json.dumps(work_order), json.dumps(plan), text_turn("Done.")]
    )
    worker = Worker(
        settings,
        store=store,
        worker_id="pg-worker",
        client_factory=lambda _run_id: client,
        backends_factory=lambda _settings: Backends(
            mail=MockMailSource(settings.mail_db, settings.shared_dir),
            accounting=InMemoryAccountingSystem(),
        ),
    )

    item = worker.run_once()

    assert item is not None and item.status == "done", item.last_error
    assert store.get_run("RUN-PG-6").state is RunState.COMPLETED
