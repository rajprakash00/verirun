"""The demo script runs the happy path, one failure, and one approval end to end."""

from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path

from company_operator.engine.states import RunState
from company_operator.runs.store import RunStore
from mocks.ledgerlite import db as ledgerlite
from scripts import demo


def test_demo_runs_happy_failure_and_approval(
    tmp_path: Path, capsys
) -> None:
    runs_dir = tmp_path / "runs"

    code = demo.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--shared",
            str(tmp_path / "shared"),
            "--runs",
            str(runs_dir),
            "--no-browser",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Demo complete" in output
    ids = dict(re.findall(r"\[(happy|transient|approval)\] (RUN-\S+):", output))
    assert set(ids) == {"happy", "transient", "approval"}

    store = RunStore(runs_dir / "operator.db")
    happy = store.get_run(ids["happy"])
    failure = store.get_run(ids["transient"])
    approval = store.get_run(ids["approval"])
    assert happy.state is RunState.COMPLETED
    assert failure.state is RunState.COMPLETED
    assert approval.state is RunState.COMPLETED

    for run_id in ids.values():
        assert (runs_dir / run_id / "evidence.json").is_file()
        assert (runs_dir / run_id / "evidence.html").is_file()

    happy_evidence = json.loads(
        (runs_dir / ids["happy"] / "evidence.json").read_text(encoding="utf-8")
    )
    assert happy_evidence["verified"] is True
    assert happy_evidence["verification"]["passed"] is True

    attempts = [
        (observation.ok, observation.attempt)
        for observation in store.list_observations(ids["transient"])
        if observation.tool == "erp.file_invoice"
    ]
    assert attempts == [(False, 1), (True, 2)]

    approvals = store.list_approvals(ids["approval"])
    assert len(approvals) == 1
    assert approvals[0].status == "approved"
    assert approvals[0].executed_at is not None

    with closing(ledgerlite.connect(tmp_path / "state" / "ledgerlite.db")) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'SI-2026-550'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert [payment["status"] for payment in payments] == ["scheduled"]
