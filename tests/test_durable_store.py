"""Durable store behaviour: outbox states, natural-key claims, and leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from verirun.engine.states import RunState
from verirun.runs.store import (
    ClaimConflictError,
    RunStore,
    UnknownActionError,
)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs.db")


def create_run(store: RunStore, run_id: str = "RUN-0001") -> str:
    store.create_run("Process the invoices", "invoice-processing", run_id=run_id)
    return run_id


def test_resolving_an_exception_settles_the_outbox_entry(store: RunStore) -> None:
    run_id = create_run(store)
    key = "step-1:erp.file_invoice:abc"
    claim_key = "bill:V-1:INV-9"
    store.record_action(
        run_id, key, "erp.file_invoice", {"number": "INV-9"}, claim_key=claim_key
    )
    store.mark_action_unknown(run_id, key, "timeout")
    exception = store.record_exception(
        run_id,
        kind="unknown_write_outcome",
        summary="timeout",
        question="Check the bill",
        evidence={"claim_key": claim_key},
        journal_key=key,
    )

    resolved = store.resolve_exception(
        exception.id,
        resolution="the bill landed",
        outcome="landed",
        result={"ok": True, "summary": "reconciled", "data": {"invoice_id": "INV-9"}},
    )

    assert resolved.status == "resolved"
    entry = store.get_journal_entry(run_id, key)
    assert entry is not None and entry.status == "done"
    assert store.get_claim(claim_key).status == "done"


def test_resolving_an_exception_as_not_landed_releases_the_claim(store: RunStore) -> None:
    run_id = create_run(store)
    key = "step-1:erp.file_invoice:def"
    claim_key = "bill:V-1:INV-10"
    store.record_action(
        run_id, key, "erp.file_invoice", {"number": "INV-10"}, claim_key=claim_key
    )
    store.mark_action_unknown(run_id, key, "timeout")
    exception = store.record_exception(
        run_id,
        kind="unknown_write_outcome",
        summary="timeout",
        question="Check the bill",
        journal_key=key,
    )

    store.resolve_exception(
        exception.id, resolution="no bill exists", outcome="not_landed"
    )

    entry = store.get_journal_entry(run_id, key)
    assert entry is not None and entry.status == "failed"
    assert store.get_claim(claim_key) is None


def test_the_evidence_pack_index_records_its_path_and_hash(store: RunStore) -> None:
    run_id = create_run(store)

    store.record_evidence_pack(run_id, f"runs/{run_id}/evidence.json", "abc123")

    record = store.get_evidence_pack(run_id)
    assert record is not None
    assert record.path.endswith("evidence.json")
    assert record.sha256 == "abc123"


def test_create_queued_run_writes_the_run_and_job_together(store: RunStore) -> None:
    run = store.create_queued_run(
        "Process the invoices", "invoice-processing", run_id="RUN-QUEUED-1"
    )

    item = store.active_queue_item(run.id, "start")
    assert run.state is RunState.CREATED
    assert item is not None and item.status == "queued"


def test_an_unknown_outcome_is_never_retried_and_records_an_exception(
    store: RunStore,
) -> None:
    run_id = create_run(store)
    store.record_action(run_id, "step-1:erp.file_invoice:abc", "erp.file_invoice", {"n": 1})

    store.mark_action_unknown(run_id, "step-1:erp.file_invoice:abc", "timeout after submit")
    exception = store.record_exception(
        run_id,
        kind="unknown_write_outcome",
        summary="timeout after submit",
        question="Check Zoho Books before resuming.",
        evidence={"claim_key": "bill:V-1:INV-9", "vendor_id": "V-1"},
        journal_key="step-1:erp.file_invoice:abc",
    )

    entry = store.get_journal_entry(run_id, "step-1:erp.file_invoice:abc")
    assert entry is not None
    assert entry.status == "unknown"
    assert [item.key for item in store.list_outbox(run_id)] == [
        "step-1:erp.file_invoice:abc"
    ]
    with pytest.raises(UnknownActionError):
        store.record_action(
            run_id, "step-1:erp.file_invoice:abc", "erp.file_invoice", {"n": 1}
        )

    assert store.open_exception(run_id) == exception
    assert exception.kind == "unknown_write_outcome"
    assert exception.evidence["claim_key"] == "bill:V-1:INV-9"
    assert store.list_open_exceptions() == [exception]

    resolved = store.resolve_exception(exception.id, resolution="bill INV-9 exists")
    assert resolved.status == "resolved"
    assert resolved.resolution == "bill INV-9 exists"
    assert store.open_exception(run_id) is None


def test_a_natural_key_is_claimed_once_and_released_on_a_definite_failure(
    store: RunStore,
) -> None:
    first = create_run(store, "RUN-0001")
    second = create_run(store, "RUN-0002")

    assert store.record_action(
        first,
        "step-1:erp.file_invoice:abc",
        "erp.file_invoice",
        {"number": "INV-9"},
        claim_key="bill:V-1:INV-9",
        claim_detail={"number": "INV-9", "vendor_id": "V-1"},
    )
    # The same Run and journal entry may resume its own claim.
    assert store.record_action(
        first,
        "step-1:erp.file_invoice:abc",
        "erp.file_invoice",
        {"number": "INV-9"},
        claim_key="bill:V-1:INV-9",
    )

    with pytest.raises(ClaimConflictError) as conflict:
        store.record_action(
            second,
            "step-2:erp.file_invoice:def",
            "erp.file_invoice",
            {"number": "INV-9"},
            claim_key="bill:V-1:INV-9",
        )
    assert conflict.value.claim.run_id == first

    store.complete_action(first, "step-1:erp.file_invoice:abc", {"ok": True})
    claim = store.get_claim("bill:V-1:INV-9")
    assert claim is not None and claim.status == "done"

    assert store.record_action(
        second,
        "step-2:erp.file_invoice:def",
        "erp.file_invoice",
        {"number": "INV-10"},
        claim_key="bill:V-1:INV-10",
    )
    store.fail_action(second, "step-2:erp.file_invoice:def", "connection refused")
    assert store.get_claim("bill:V-1:INV-10") is None


def test_queue_claims_are_exclusive_and_reused_while_active(store: RunStore) -> None:
    run_id = create_run(store)

    first = store.enqueue(run_id, "start")
    second = store.enqueue(run_id, "start")
    assert second.id == first.id

    claimed = store.claim("worker-a")
    assert claimed is not None
    assert claimed.run_id == run_id
    assert claimed.status == "leased"
    assert claimed.attempts == 1
    assert store.claim("worker-b") is None

    finished = store.complete_queue_item(claimed.id, "worker-a")
    assert finished.status == "done"


def test_an_expired_lease_is_reclaimed_by_another_worker(store: RunStore) -> None:
    run_id = create_run(store)
    start = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    store.enqueue(run_id, "start", now=start)

    first = store.claim("worker-a", lease_seconds=10, now=start)
    assert first is not None
    assert store.claim("worker-b", lease_seconds=10, now=start + timedelta(seconds=5)) is None

    second = store.claim(
        "worker-b", lease_seconds=10, now=start + timedelta(seconds=11)
    )
    assert second is not None
    assert second.id == first.id
    assert second.attempts == 2
    assert second.lease_owner == "worker-b"


def test_exhausted_queue_items_move_to_the_dead_letter_state(store: RunStore) -> None:
    run_id = create_run(store)
    store.enqueue(run_id, "resume", max_attempts=1)

    claimed = store.claim("worker-a")
    assert claimed is not None
    failed = store.fail_queue_item(claimed.id, "worker-a", "database went away")

    assert failed.status == "dead"
    assert failed.last_error == "database went away"
    assert [item.id for item in store.dead_letter_queue()] == [claimed.id]
    assert store.claim("worker-b") is None


def test_a_transient_queue_failure_is_requeued_with_backoff(store: RunStore) -> None:
    run_id = create_run(store)
    start = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    store.enqueue(run_id, "resume", max_attempts=3, now=start)

    claimed = store.claim("worker-a", lease_seconds=10, now=start)
    assert claimed is not None
    failed = store.fail_queue_item(
        claimed.id, "worker-a", "temporary", now=start, backoff_seconds=30
    )

    assert failed.status == "queued"
    assert failed.available_at == start + timedelta(seconds=30)
    assert store.claim("worker-b", now=start + timedelta(seconds=29)) is None
    reclaimed = store.claim("worker-b", now=start + timedelta(seconds=30))
    assert reclaimed is not None
    assert reclaimed.attempts == 2


def test_renewing_a_lease_keeps_the_claim_alive(store: RunStore) -> None:
    run_id = create_run(store)
    start = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    store.enqueue(run_id, "start", now=start)
    claimed = store.claim("worker-a", lease_seconds=10, now=start)
    assert claimed is not None

    assert store.renew_lease(
        claimed.id, "worker-a", lease_seconds=60, now=start + timedelta(seconds=5)
    )
    assert store.claim("worker-b", now=start + timedelta(seconds=30)) is None


def test_enqueuing_a_resume_for_an_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(KeyError):
        store.enqueue("RUN-NOPE", "resume")
