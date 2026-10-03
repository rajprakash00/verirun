from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from company_operator.context.models import WorkOrder
from company_operator.engine.models import CheckResult, Observation, Plan, Step
from company_operator.engine.states import RunState, StepState, transition
from company_operator.runs.models import (
    ApprovalRequest,
    Checkpoint,
    Escalation,
    JournalEntry,
    ObservationRecord,
    Run,
    RunSummary,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    work_order_json TEXT,
    plan_json TEXT,
    error TEXT,
    steps_used INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    id TEXT NOT NULL,
    goal TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL,
    done_criterion TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (run_id, position)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_position INTEGER,
    tool TEXT,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    error_kind TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    attempt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    question TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    step_position INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    policy TEXT,
    rule TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decision_reason TEXT,
    decided_at TEXT,
    executed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, key)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, key)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    label TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, label)
);

CREATE TABLE IF NOT EXISTS verifications (
    run_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    id TEXT NOT NULL,
    description TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, position)
);
"""


class RunNotFoundError(KeyError):
    pass


class ApprovalNotFoundError(KeyError):
    pass


class ApprovalDecisionError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def generate_run_id(now: datetime | None = None) -> str:
    moment = now or _now()
    return f"RUN-{moment:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


class RunStore:
    """SQLite-backed store for Runs and everything a Run produces."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first schema to existing databases."""
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "steps_used" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN steps_used INTEGER NOT NULL DEFAULT 0")
        if "cost_usd" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0.0")
        observation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(observations)")
        }
        if "tool" not in observation_columns:
            conn.execute("ALTER TABLE observations ADD COLUMN tool TEXT")
        if "attempt" not in observation_columns:
            conn.execute(
                "ALTER TABLE observations ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
            )

    def create_run(self, request: str, task_id: str, run_id: str | None = None) -> Run:
        run_id = run_id or generate_run_id()
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO runs (id, request, task_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, request, task_id, RunState.CREATED.value, moment, moment),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no Run with id {run_id}")
        return _row_to_run(row)

    def list_runs(self) -> list[RunSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT runs.*, (
                    SELECT question FROM escalations
                    WHERE escalations.run_id = runs.id AND escalations.resolved = 0
                    ORDER BY escalations.id DESC LIMIT 1
                ) AS open_question
                FROM runs ORDER BY runs.created_at, runs.id
                """
            ).fetchall()
        return [
            RunSummary(
                id=row["id"],
                task_id=row["task_id"],
                request=row["request"],
                state=RunState(row["state"]),
                question=row["open_question"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def list_escalated_runs(self) -> list[RunSummary]:
        """Runs parked for a human, each with its open question."""
        return [
            summary
            for summary in self.list_runs()
            if summary.state is RunState.NEEDS_HUMAN and summary.question is not None
        ]

    def escalate(
        self,
        run_id: str,
        *,
        reason: str,
        question: str,
        context: dict[str, Any] | None = None,
    ) -> Escalation:
        """Record an open question and park the Run in ``needs_human``."""
        run = self.get_run(run_id)
        transition(run.state, RunState.NEEDS_HUMAN)
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO escalations (run_id, reason, question, context_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, reason, question, json.dumps(context or {}), moment),
            )
            escalation_id = int(cursor.lastrowid)
        self.transition(run_id, RunState.NEEDS_HUMAN)
        return Escalation(
            id=escalation_id,
            run_id=run_id,
            reason=reason,
            question=question,
            context=context or {},
            resolved=False,
            created_at=datetime.fromisoformat(moment),
        )

    def resolve_escalations(self, run_id: str) -> None:
        """Clear every open question for a Run, as when a human has answered."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE escalations SET resolved = 1 WHERE run_id = ? AND resolved = 0",
                (run_id,),
            )

    def open_escalation(self, run_id: str) -> Escalation | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM escalations WHERE run_id = ? AND resolved = 0
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _row_to_escalation(row) if row is not None else None

    def list_escalations(self, run_id: str) -> list[Escalation]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM escalations WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_escalation(row) for row in rows]

    def create_approval(
        self,
        run_id: str,
        *,
        key: str,
        step_position: int,
        step_id: str,
        tool: str,
        action: str,
        arguments: dict[str, Any] | None = None,
        policy: str | None = None,
        rule: str | None = None,
        reason: str = "",
    ) -> ApprovalRequest:
        """Prepare one irreversible action for a human decision.

        Creating the same prepared action twice returns the existing request, so
        a resumed Run never raises a duplicate gate.
        """
        self._require(run_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND key = ?", (run_id, key)
            ).fetchone()
        if row is not None:
            return _row_to_approval(row)
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals
                    (run_id, key, step_position, step_id, tool, action, arguments_json,
                     policy, rule, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    run_id,
                    key,
                    step_position,
                    step_id,
                    tool,
                    action,
                    json.dumps(arguments or {}),
                    policy,
                    rule,
                    reason,
                    moment,
                ),
            )
        return self.get_approval(int(cursor.lastrowid))

    def get_approval(self, approval_id: int) -> ApprovalRequest:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"no Approval Request with id {approval_id}")
        return _row_to_approval(row)

    def list_approvals(self, run_id: str) -> list[ApprovalRequest]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        """Every prepared action across all Runs, oldest first: the queue."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def unsubmitted_approvals(self, run_id: str, step_position: int) -> list[ApprovalRequest]:
        """Approved prepared actions for one Step that the engine has not submitted."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ? AND step_position = ? AND status = 'approved'
                      AND executed_at IS NULL
                ORDER BY id
                """,
                (run_id, step_position),
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def open_approval(self, run_id: str) -> ApprovalRequest | None:
        """The oldest pending Approval Request for a Run, if one exists."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM approvals WHERE run_id = ? AND status = 'pending'
                ORDER BY id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def approved_approval_for(self, run_id: str, key: str) -> ApprovalRequest | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ? AND key = ? AND status = 'approved'
                ORDER BY id LIMIT 1
                """,
                (run_id, key),
            ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def decide_approval(
        self, approval_id: int, *, approved: bool, reason: str | None = None
    ) -> ApprovalRequest:
        """Record a human decision. A decided request cannot be decided again."""
        request = self.get_approval(approval_id)
        if request.status != "pending":
            raise ApprovalDecisionError(
                f"Approval Request {approval_id} was already {request.status}"
            )
        status = "approved" if approved else "rejected"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE approvals SET status = ?, decision_reason = ?, decided_at = ?
                WHERE id = ?
                """,
                (status, reason, _iso(_now()), approval_id),
            )
        return self.get_approval(approval_id)

    def mark_approval_executed(self, approval_id: int) -> ApprovalRequest:
        """Record that the approved action was submitted, exactly once."""
        request = self.get_approval(approval_id)
        if request.status != "approved":
            raise ApprovalDecisionError(
                f"Approval Request {approval_id} is {request.status}, not approved"
            )
        if request.executed_at is not None:
            raise ApprovalDecisionError(f"Approval Request {approval_id} was already executed")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE approvals SET executed_at = ? WHERE id = ?",
                (_iso(_now()), approval_id),
            )
        return self.get_approval(approval_id)

    def transition(self, run_id: str, target: RunState) -> Run:
        run = self.get_run(run_id)
        new_state = transition(run.state, target)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE id = ?",
                (new_state.value, _iso(_now()), run_id),
            )
        return self.get_run(run_id)

    def save_work_order(self, run_id: str, work_order: WorkOrder) -> None:
        self._update_run(run_id, "work_order_json = ?", (work_order.model_dump_json(),))

    def save_plan(self, run_id: str, plan: Plan) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE runs SET plan_json = ?, updated_at = ? WHERE id = ?",
                (plan.model_dump_json(), _iso(_now()), run_id),
            )
            conn.execute("DELETE FROM steps WHERE run_id = ?", (run_id,))
            for position, step in enumerate(plan.steps):
                conn.execute(
                    """
                    INSERT INTO steps (run_id, position, id, goal, allowed_tools_json,
                                       done_criterion, state)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        position,
                        step.id,
                        step.goal,
                        json.dumps(step.allowed_tools),
                        step.done_criterion,
                        StepState.PENDING.value,
                    ),
                )
        self._require(run_id)

    def get_steps(self, run_id: str) -> list[Step]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [
            Step(
                id=row["id"],
                goal=row["goal"],
                allowed_tools=json.loads(row["allowed_tools_json"]),
                done_criterion=row["done_criterion"],
            )
            for row in rows
        ]

    def set_step_state(self, run_id: str, position: int, state: StepState) -> None:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE steps SET state = ? WHERE run_id = ? AND position = ?",
                (state.value, run_id, position),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no Step at position {position} for Run {run_id}")

    def get_step_states(self, run_id: str) -> list[StepState]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT state FROM steps WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [StepState(row["state"]) for row in rows]

    def set_error(self, run_id: str, message: str) -> None:
        self._update_run(run_id, "error = ?", (message,))

    def increment_usage(
        self, run_id: str, *, steps: int = 0, cost_usd: float = 0.0
    ) -> None:
        self._require(run_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE runs
                SET steps_used = steps_used + ?, cost_usd = cost_usd + ?, updated_at = ?
                WHERE id = ?
                """,
                (steps, cost_usd, _iso(_now()), run_id),
            )

    def add_observation(
        self,
        run_id: str,
        observation: Observation,
        step_position: int | None = None,
        tool: str | None = None,
        attempt: int = 1,
    ) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO observations (run_id, step_position, tool, ok, summary, data_json,
                                          error_kind, artifacts_json, attempt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_position,
                    tool,
                    int(observation.ok),
                    observation.summary,
                    json.dumps(observation.data),
                    observation.error_kind,
                    json.dumps(observation.artifacts),
                    attempt,
                    _iso(_now()),
                ),
            )
        return int(cursor.lastrowid)

    def list_observations(self, run_id: str) -> list[ObservationRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            ObservationRecord(
                id=row["id"],
                run_id=row["run_id"],
                step_position=row["step_position"],
                tool=row["tool"],
                ok=bool(row["ok"]),
                summary=row["summary"],
                data=json.loads(row["data_json"]),
                error_kind=row["error_kind"],
                artifacts=json.loads(row["artifacts_json"]),
                attempt=row["attempt"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def record_action(
        self, run_id: str, key: str, action: str, payload: dict[str, Any] | None = None
    ) -> bool:
        """Claim an idempotency key. False when the action already completed."""
        payload_json = json.dumps(payload or {})
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT status FROM journal WHERE run_id = ? AND key = ?", (run_id, key)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO journal (run_id, key, action, payload_json, status,
                                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'intent', ?, ?)
                    """,
                    (run_id, key, action, payload_json, moment, moment),
                )
                return True
            if row["status"] == "done":
                return False
            conn.execute(
                """
                UPDATE journal SET action = ?, payload_json = ?, status = 'intent',
                                   error = NULL, updated_at = ?
                WHERE run_id = ? AND key = ?
                """,
                (action, payload_json, moment, run_id, key),
            )
            return True

    def complete_action(
        self, run_id: str, key: str, result: dict[str, Any] | None = None
    ) -> None:
        self._update_journal(
            run_id, key, "status = 'done', result_json = ?, error = NULL", json.dumps(result or {})
        )

    def fail_action(self, run_id: str, key: str, error: str) -> None:
        self._update_journal(run_id, key, "status = 'failed', error = ?", error)

    def completed_action(self, run_id: str, key: str) -> bool:
        entry = self.get_journal_entry(run_id, key)
        return entry is not None and entry.status == "done"

    def get_journal_entry(self, run_id: str, key: str) -> JournalEntry | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM journal WHERE run_id = ? AND key = ?", (run_id, key)
            ).fetchone()
        return _row_to_journal(row) if row is not None else None

    def list_journal(self, run_id: str) -> list[JournalEntry]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM journal WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_journal(row) for row in rows]

    def save_checkpoint(self, run_id: str, label: str, state: dict[str, Any]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO checkpoints (run_id, label, state_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (run_id, label) DO UPDATE
                    SET state_json = excluded.state_json, created_at = excluded.created_at
                """,
                (run_id, label, json.dumps(state), _iso(_now())),
            )

    def count_checkpoints(self, run_id: str, *, prefix: str = "") -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM checkpoints WHERE run_id = ? AND label LIKE ?",
                (run_id, f"{prefix}%"),
            ).fetchone()
        return int(row["total"])

    def get_checkpoint(self, run_id: str, label: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? AND label = ?", (run_id, label)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM checkpoints WHERE run_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            run_id=row["run_id"],
            label=row["label"],
            state=json.loads(row["state_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def save_verification(self, run_id: str, results: list[CheckResult]) -> None:
        """Replace the Run's verification results with this Verifier pass."""
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM verifications WHERE run_id = ?", (run_id,))
            for position, result in enumerate(results):
                conn.execute(
                    """
                    INSERT INTO verifications
                        (run_id, position, id, description, ok, detail, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        position,
                        result.id,
                        result.description,
                        int(result.ok),
                        result.detail,
                        json.dumps(result.evidence),
                        _iso(_now()),
                    ),
                )

    def get_verification(self, run_id: str) -> list[CheckResult]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM verifications WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [
            CheckResult(
                id=row["id"],
                description=row["description"],
                ok=bool(row["ok"]),
                detail=row["detail"],
                evidence=json.loads(row["evidence_json"]),
            )
            for row in rows
        ]

    def _update_run(self, run_id: str, assignments: str, params: tuple[Any, ...]) -> None:
        self._require(run_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, _iso(_now()), run_id),
            )

    def _update_journal(
        self, run_id: str, key: str, assignments: str, *params: Any
    ) -> None:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                f"UPDATE journal SET {assignments}, updated_at = ? WHERE run_id = ? AND key = ?",
                (*params, _iso(_now()), run_id, key),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no journal entry '{key}' for Run {run_id}")

    def _require(self, run_id: str) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no Run with id {run_id}")


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        request=row["request"],
        task_id=row["task_id"],
        state=RunState(row["state"]),
        work_order=WorkOrder.model_validate_json(row["work_order_json"])
        if row["work_order_json"]
        else None,
        plan=Plan.model_validate_json(row["plan_json"]) if row["plan_json"] else None,
        error=row["error"],
        steps_used=row["steps_used"],
        cost_usd=row["cost_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_escalation(row: sqlite3.Row) -> Escalation:
    return Escalation(
        id=row["id"],
        run_id=row["run_id"],
        reason=row["reason"],
        question=row["question"],
        context=json.loads(row["context_json"]),
        resolved=bool(row["resolved"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        run_id=row["run_id"],
        key=row["key"],
        step_position=row["step_position"],
        step_id=row["step_id"],
        tool=row["tool"],
        action=row["action"],
        arguments=json.loads(row["arguments_json"]),
        policy=row["policy"],
        rule=row["rule"],
        reason=row["reason"],
        status=row["status"],
        decision_reason=row["decision_reason"],
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_journal(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        id=row["id"],
        run_id=row["run_id"],
        key=row["key"],
        action=row["action"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
