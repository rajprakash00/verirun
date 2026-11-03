"""Durable Run state on SQLite (demo/tests) or PostgreSQL (product).

The journal is the Run's outbox. ``intent`` is committed before an external
side effect, ``done`` after it, and ``unknown`` when the effect may have landed
but the outcome could not be established. An ``unknown`` entry is never retried
automatically; it parks the Run on an Exception until a human reconciles it.

The work queue stores one claimable job per active Run. A worker leases a job,
keeps the lease while it runs, and a crash leaves the job leased until the lease
expires and another worker can claim it. SQLite serialises claims in a write
transaction; PostgreSQL uses ``FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from verirun.context.models import WorkOrder
from verirun.engine.models import CheckResult, Observation, Plan, Step
from verirun.engine.states import RunState, StepState, transition
from verirun.runs.db import Database, open_database
from verirun.runs.errors import ClaimConflictError, UnknownActionError
from verirun.runs.models import (
    ApprovalRequest,
    Checkpoint,
    Escalation,
    EvidencePackRecord,
    JournalEntry,
    ObservationRecord,
    QueueItem,
    Run,
    RunException,
    RunSummary,
    SideEffectClaim,
)

DEFAULT_LEASE_SECONDS = 900
DEFAULT_MAX_ATTEMPTS = 3
CLAIM_BACKOFF_SECONDS = 30


class RunNotFoundError(KeyError):
    pass


class ApprovalNotFoundError(KeyError):
    pass


class ApprovalDecisionError(ValueError):
    pass


class ExceptionNotFoundError(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def generate_run_id(now: datetime | None = None) -> str:
    moment = now or _now()
    return f"RUN-{moment:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


class RunStore:
    """The durable store for Runs, their outbox, exceptions, and work queue."""

    def __init__(self, target: str | Path) -> None:
        self._database: Database = open_database(target)
        self.path = getattr(self._database, "path", Path(str(target)))
        with closing(self._connect()) as conn, conn:
            self._database.initialize(conn)

    @property
    def kind(self) -> str:
        return self._database.kind

    @classmethod
    def for_settings(cls, settings: Any) -> RunStore:
        """The product store when a database URL is configured, else SQLite."""
        return cls(settings.database_url or settings.run_db)

    def _connect(self) -> Any:
        return self._database.connect()

    def _insert(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        return self._database.insert(conn, sql, params)

    def _execute(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return conn.execute(self._database.prepare(sql), params)

    # Runs -----------------------------------------------------------------

    def create_run(self, request: str, task_id: str, run_id: str | None = None) -> Run:
        run_id = run_id or generate_run_id()
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                INSERT INTO runs (id, request, task_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, request, task_id, RunState.CREATED.value, moment, moment),
            )
        return self.get_run(run_id)

    def create_queued_run(
        self,
        request: str,
        task_id: str,
        *,
        kind: str = "start",
        run_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: datetime | None = None,
    ) -> Run:
        """Create a Run and its first queue job in one transaction.

        A crash between the two inserts must not leave a ``created`` Run with no
        worker job, so they commit together.
        """
        run_id = run_id or generate_run_id()
        moment = now or _now()
        moment_iso = _iso(moment)
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                INSERT INTO runs (id, request, task_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, request, task_id, RunState.CREATED.value, moment_iso, moment_iso),
            )
            self._insert(
                conn,
                """
                INSERT INTO work_queue
                    (run_id, kind, status, attempts, max_attempts, available_at,
                     created_at, updated_at)
                VALUES (?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    max_attempts,
                    moment_iso,
                    moment_iso,
                    moment_iso,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with closing(self._connect()) as conn:
            row = self._execute(conn, "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no Run with id {run_id}")
        return _row_to_run(row)

    def list_runs(self) -> list[RunSummary]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
                """
                SELECT runs.*, (
                    SELECT question FROM escalations
                    WHERE escalations.run_id = runs.id AND escalations.resolved = 0
                    ORDER BY escalations.id DESC LIMIT 1
                ) AS open_question
                FROM runs ORDER BY runs.created_at, runs.id
                """,
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
            escalation_id = self._insert(
                conn,
                """
                INSERT INTO escalations (run_id, reason, question, context_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, reason, question, json.dumps(context or {}), moment),
            )
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
            self._execute(
                conn,
                "UPDATE escalations SET resolved = 1 WHERE run_id = ? AND resolved = 0",
                (run_id,),
            )

    def open_escalation(self, run_id: str) -> Escalation | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn,
                """
                SELECT * FROM escalations WHERE run_id = ? AND resolved = 0
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _row_to_escalation(row) if row is not None else None

    def list_escalations(self, run_id: str) -> list[Escalation]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM escalations WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [_row_to_escalation(row) for row in rows]

    # Approvals ------------------------------------------------------------

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
            row = self._execute(
                conn,
                "SELECT * FROM approvals WHERE run_id = ? AND key = ?",
                (run_id, key),
            ).fetchone()
        if row is not None:
            return _row_to_approval(row)
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            approval_id = self._insert(
                conn,
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
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: int) -> ApprovalRequest:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"no Approval Request with id {approval_id}")
        return _row_to_approval(row)

    def list_approvals(self, run_id: str) -> list[ApprovalRequest]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn, "SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        """Every prepared action across all Runs, oldest first: the queue."""
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn, "SELECT * FROM approvals WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def unsubmitted_approvals(self, run_id: str, step_position: int) -> list[ApprovalRequest]:
        """Approved prepared actions for one Step that the engine has not submitted."""
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
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
            row = self._execute(
                conn,
                """
                SELECT * FROM approvals WHERE run_id = ? AND status = 'pending'
                ORDER BY id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _row_to_approval(row) if row is not None else None

    def approved_approval_for(self, run_id: str, key: str) -> ApprovalRequest | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn,
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
            self._execute(
                conn,
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
            self._execute(
                conn,
                "UPDATE approvals SET executed_at = ? WHERE id = ?",
                (_iso(_now()), approval_id),
            )
        return self.get_approval(approval_id)

    # State and plan -------------------------------------------------------

    def transition(self, run_id: str, target: RunState) -> Run:
        run = self.get_run(run_id)
        new_state = transition(run.state, target)
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                "UPDATE runs SET state = ?, updated_at = ? WHERE id = ?",
                (new_state.value, _iso(_now()), run_id),
            )
        return self.get_run(run_id)

    def save_work_order(self, run_id: str, work_order: WorkOrder) -> None:
        self._update_run(run_id, "work_order_json = ?", (work_order.model_dump_json(),))

    def save_plan(self, run_id: str, plan: Plan) -> None:
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                "UPDATE runs SET plan_json = ?, updated_at = ? WHERE id = ?",
                (plan.model_dump_json(), _iso(_now()), run_id),
            )
            self._execute(conn, "DELETE FROM steps WHERE run_id = ?", (run_id,))
            for position, step in enumerate(plan.steps):
                self._execute(
                    conn,
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
            rows = self._execute(
                conn, "SELECT * FROM steps WHERE run_id = ? ORDER BY position", (run_id,)
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
            cursor = self._execute(
                conn,
                "UPDATE steps SET state = ? WHERE run_id = ? AND position = ?",
                (state.value, run_id, position),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no Step at position {position} for Run {run_id}")

    def get_step_states(self, run_id: str) -> list[StepState]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn, "SELECT state FROM steps WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [StepState(row["state"]) for row in rows]

    def set_error(self, run_id: str, message: str) -> None:
        self._update_run(run_id, "error = ?", (message,))

    def increment_usage(
        self, run_id: str, *, steps: int = 0, cost_usd: float = 0.0
    ) -> None:
        self._require(run_id)
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
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
            observation_id = self._insert(
                conn,
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
        return observation_id

    def list_observations(self, run_id: str) -> list[ObservationRecord]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn, "SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,)
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

    # The outbox: journal and natural-key claims ---------------------------

    def record_action(
        self,
        run_id: str,
        key: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        claim_key: str | None = None,
        claim_detail: dict[str, Any] | None = None,
    ) -> bool:
        """Commit an ``intent`` for one side effect before it is invoked.

        The intent and its natural-key claim are written in one transaction, so
        a crash can never leave a claimed bill without an outbox entry (or the
        reverse). Returns False when the action already completed; raises
        :class:`UnknownActionError` when the outcome is unknown, and
        :class:`ClaimConflictError` when another Run owns the natural key.
        """
        payload_json = json.dumps(payload or {})
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            row = self._execute(
                conn,
                "SELECT * FROM journal WHERE run_id = ? AND key = ?",
                (run_id, key),
            ).fetchone()
            if row is not None and row["status"] == "done":
                return False
            if row is not None and row["status"] == "unknown":
                raise UnknownActionError(
                    f"journal entry '{key}' for Run {run_id} has an unknown outcome"
                )
            if row is None:
                self._insert(
                    conn,
                    """
                    INSERT INTO journal (run_id, key, action, payload_json, status,
                                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'intent', ?, ?)
                    """,
                    (run_id, key, action, payload_json, moment, moment),
                )
            else:
                self._execute(
                    conn,
                    """
                    UPDATE journal SET action = ?, payload_json = ?, status = 'intent',
                                       error = NULL, updated_at = ?
                    WHERE run_id = ? AND key = ?
                    """,
                    (action, payload_json, moment, run_id, key),
                )
            if claim_key is not None:
                self._claim(conn, run_id, key, claim_key, claim_detail, moment)
            return True

    def _claim(
        self,
        conn: Any,
        run_id: str,
        journal_key: str,
        claim_key: str,
        detail: dict[str, Any] | None,
        moment: str,
    ) -> None:
        row = self._execute(
            conn, "SELECT * FROM side_effect_claims WHERE claim_key = ?", (claim_key,)
        ).fetchone()
        if row is not None:
            if row["run_id"] != run_id or row["journal_key"] != journal_key:
                raise ClaimConflictError(_row_to_claim(row))
            return
        self._execute(
            conn,
            """
            INSERT INTO side_effect_claims
                (claim_key, run_id, journal_key, status, detail_json, created_at, updated_at)
            VALUES (?, ?, ?, 'claimed', ?, ?, ?)
            """,
            (claim_key, run_id, journal_key, json.dumps(detail or {}), moment, moment),
        )

    def complete_action(
        self, run_id: str, key: str, result: dict[str, Any] | None = None
    ) -> None:
        with closing(self._connect()) as conn, conn:
            self._update_journal(
                conn,
                run_id,
                key,
                "status = 'done', result_json = ?, error = NULL",
                json.dumps(result or {}),
            )
            self._execute(
                conn,
                """
                UPDATE side_effect_claims SET status = 'done', updated_at = ?
                WHERE run_id = ? AND journal_key = ?
                """,
                (_iso(_now()), run_id, key),
            )

    def fail_action(self, run_id: str, key: str, error: str) -> None:
        with closing(self._connect()) as conn, conn:
            self._update_journal(conn, run_id, key, "status = 'failed', error = ?", error)
            # A definite failure wrote nothing, so the natural key is free for
            # another Run until this one retries.
            self._execute(
                conn,
                "DELETE FROM side_effect_claims WHERE run_id = ? AND journal_key = ?",
                (run_id, key),
            )

    def mark_action_unknown(self, run_id: str, key: str, error: str) -> None:
        """Record that a write may have landed; it must never auto-retry."""
        with closing(self._connect()) as conn, conn:
            self._update_journal(conn, run_id, key, "status = 'unknown', error = ?", error)

    def completed_action(self, run_id: str, key: str) -> bool:
        entry = self.get_journal_entry(run_id, key)
        return entry is not None and entry.status == "done"

    def get_journal_entry(self, run_id: str, key: str) -> JournalEntry | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM journal WHERE run_id = ? AND key = ?", (run_id, key)
            ).fetchone()
        return _row_to_journal(row) if row is not None else None

    def list_journal(self, run_id: str) -> list[JournalEntry]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn, "SELECT * FROM journal WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_journal(row) for row in rows]

    def list_outbox(self, run_id: str) -> list[JournalEntry]:
        """Unresolved outbox entries: an intent to reconcile, or an unknown."""
        return [
            entry
            for entry in self.list_journal(run_id)
            if entry.status in ("intent", "unknown")
        ]

    def get_claim(self, claim_key: str) -> SideEffectClaim | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM side_effect_claims WHERE claim_key = ?", (claim_key,)
            ).fetchone()
        return _row_to_claim(row) if row is not None else None

    def list_claims(self, run_id: str) -> list[SideEffectClaim]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM side_effect_claims WHERE run_id = ? ORDER BY claim_key",
                (run_id,),
            ).fetchall()
        return [_row_to_claim(row) for row in rows]

    # Exceptions -----------------------------------------------------------

    def record_exception(
        self,
        run_id: str,
        *,
        kind: str,
        summary: str,
        question: str,
        evidence: dict[str, Any] | None = None,
        journal_key: str | None = None,
    ) -> RunException:
        """Record the evidence a human needs, once per Run journal entry."""
        if journal_key is not None:
            existing = self.exception_for(run_id, journal_key, kind)
            if existing is not None:
                return existing
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            exception_id = self._insert(
                conn,
                """
                INSERT INTO run_exceptions
                    (run_id, journal_key, kind, summary, question, evidence_json,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    run_id,
                    journal_key,
                    kind,
                    summary,
                    question,
                    json.dumps(evidence or {}),
                    moment,
                ),
            )
        return self.get_exception(exception_id)

    def get_exception(self, exception_id: int) -> RunException:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM run_exceptions WHERE id = ?", (exception_id,)
            ).fetchone()
        if row is None:
            raise ExceptionNotFoundError(f"no Exception with id {exception_id}")
        return _row_to_exception(row)

    def exception_for(
        self, run_id: str, journal_key: str, kind: str
    ) -> RunException | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn,
                """
                SELECT * FROM run_exceptions
                WHERE run_id = ? AND journal_key = ? AND kind = ?
                ORDER BY id DESC LIMIT 1
                """,
                (run_id, journal_key, kind),
            ).fetchone()
        return _row_to_exception(row) if row is not None else None

    def list_exceptions(self, run_id: str) -> list[RunException]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM run_exceptions WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [_row_to_exception(row) for row in rows]

    def list_open_exceptions(self) -> list[RunException]:
        with closing(self._connect()) as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM run_exceptions WHERE status = 'open' ORDER BY id",
            ).fetchall()
        return [_row_to_exception(row) for row in rows]

    def open_exception(self, run_id: str) -> RunException | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn,
                """
                SELECT * FROM run_exceptions
                WHERE run_id = ? AND status = 'open'
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _row_to_exception(row) if row is not None else None

    def resolve_exception(
        self,
        exception_id: int,
        *,
        resolution: str,
        outcome: Literal["landed", "not_landed", "acknowledged"] = "acknowledged",
        result: dict[str, Any] | None = None,
    ) -> RunException:
        """Close an Exception, optionally settling its outbox entry.

        ``landed`` records the result the human found and marks the action done;
        ``not_landed`` marks the action failed and releases its natural-key claim
        so a retry is safe; ``acknowledged`` only closes the exception.
        """
        exception = self.get_exception(exception_id)
        if exception.status != "open":
            raise ValueError(f"Exception {exception_id} is already {exception.status}")
        if outcome not in ("landed", "not_landed", "acknowledged"):
            raise ValueError(f"unknown Exception outcome {outcome!r}")
        if exception.journal_key:
            if outcome == "landed":
                if not result:
                    raise ValueError("outcome 'landed' needs the Observation result")
                self.complete_action(exception.run_id, exception.journal_key, result)
            elif outcome == "not_landed":
                self.fail_action(
                    exception.run_id,
                    exception.journal_key,
                    f"human reconciliation: {resolution}",
                )
        moment = _iso(_now())
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                UPDATE run_exceptions SET status = 'resolved', resolution = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolution, moment, exception_id),
            )
        return self.get_exception(exception_id)

    # Evidence Pack index --------------------------------------------------

    def record_evidence_pack(self, run_id: str, path: str, sha256: str) -> None:
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                INSERT INTO evidence_packs (run_id, path, sha256, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE
                    SET path = excluded.path, sha256 = excluded.sha256,
                        created_at = excluded.created_at
                """,
                (run_id, path, sha256, _iso(_now())),
            )

    def get_evidence_pack(self, run_id: str) -> EvidencePackRecord | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM evidence_packs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return EvidencePackRecord(
            run_id=row["run_id"],
            path=row["path"],
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # Checkpoints and verification ----------------------------------------

    def save_checkpoint(self, run_id: str, label: str, state: dict[str, Any]) -> None:
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
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
            row = self._execute(
                conn,
                "SELECT COUNT(*) AS total FROM checkpoints WHERE run_id = ? AND label LIKE ?",
                (run_id, f"{prefix}%"),
            ).fetchone()
        return int(row["total"])

    def get_checkpoint(self, run_id: str, label: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM checkpoints WHERE run_id = ? AND label = ?", (run_id, label)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn,
                """
                SELECT * FROM checkpoints WHERE run_id = ?
                ORDER BY created_at DESC, label DESC LIMIT 1
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
            self._execute(conn, "DELETE FROM verifications WHERE run_id = ?", (run_id,))
            for position, result in enumerate(results):
                self._execute(
                    conn,
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
            rows = self._execute(
                conn,
                "SELECT * FROM verifications WHERE run_id = ? ORDER BY position",
                (run_id,),
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

    # Work queue -----------------------------------------------------------

    def enqueue(
        self,
        run_id: str,
        kind: str = "resume",
        *,
        available_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: datetime | None = None,
    ) -> QueueItem:
        """Queue one start or resume job, reusing an active identical job."""
        if kind not in ("start", "resume"):
            raise ValueError(f"unknown queue job kind {kind!r}")
        self._require(run_id)
        moment = now or _now()
        active = self.active_queue_item(run_id, kind)
        if active is not None:
            return active
        try:
            with closing(self._connect()) as conn, conn:
                item_id = self._insert(
                    conn,
                    """
                    INSERT INTO work_queue
                        (run_id, kind, status, attempts, max_attempts, available_at,
                         created_at, updated_at)
                    VALUES (?, ?, 'queued', 0, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        kind,
                        max_attempts,
                        _iso(available_at or moment),
                        _iso(moment),
                        _iso(moment),
                    ),
                )
        except Exception:
            # A concurrent enqueue may have won the partial unique index.
            active = self.active_queue_item(run_id, kind)
            if active is not None:
                return active
            raise
        return self.get_queue_item(item_id)

    def active_queue_item(self, run_id: str, kind: str | None = None) -> QueueItem | None:
        sql = "SELECT * FROM work_queue WHERE run_id = ? AND status IN ('queued', 'leased')"
        params: tuple[Any, ...] = (run_id,)
        if kind is not None:
            sql += " AND kind = ?"
            params = (run_id, kind)
        sql += " ORDER BY id DESC LIMIT 1"
        with closing(self._connect()) as conn:
            row = self._execute(conn, sql, params).fetchone()
        return _row_to_queue(row) if row is not None else None

    def get_queue_item(self, item_id: int) -> QueueItem:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT * FROM work_queue WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"no queue item with id {item_id}")
        return _row_to_queue(row)

    def list_queue(self, *, status: str | None = None) -> list[QueueItem]:
        sql = "SELECT * FROM work_queue"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        with closing(self._connect()) as conn:
            rows = self._execute(conn, sql, params).fetchall()
        return [_row_to_queue(row) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> QueueItem | None:
        """Lease the oldest claimable job, including expired leases."""
        moment = now or _now()
        moment_iso = _iso(moment)
        expires = _iso(moment + timedelta(seconds=lease_seconds))
        with closing(self._connect()) as conn:
            try:
                self._database.begin_claim(conn)
                self._execute(
                    conn,
                    """
                    UPDATE work_queue SET status = 'dead', lease_owner = NULL,
                        lease_expires_at = NULL, last_error = 'lease expired after max attempts',
                        updated_at = ?
                    WHERE status = 'leased' AND lease_expires_at <= ?
                          AND attempts >= max_attempts
                    """,
                    (moment_iso, moment_iso),
                )
                lock = " FOR UPDATE SKIP LOCKED" if self._database.kind == "postgres" else ""
                row = self._execute(
                    conn,
                    f"""
                    SELECT * FROM work_queue
                    WHERE attempts < max_attempts
                      AND (
                        (status = 'queued' AND available_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                      )
                    ORDER BY id LIMIT 1{lock}
                    """,
                    (moment_iso, moment_iso),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                self._execute(
                    conn,
                    """
                    UPDATE work_queue
                    SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                        attempts = attempts + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (worker_id, expires, moment_iso, row["id"]),
                )
                conn.commit()
                row = self._execute(
                    conn, "SELECT * FROM work_queue WHERE id = ?", (row["id"],)
                ).fetchone()
                return _row_to_queue(row)
            except BaseException:
                conn.rollback()
                raise

    def renew_lease(
        self,
        item_id: int,
        worker_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        moment = now or _now()
        with closing(self._connect()) as conn, conn:
            cursor = self._execute(
                conn,
                """
                UPDATE work_queue SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND status = 'leased'
                """,
                (
                    _iso(moment + timedelta(seconds=lease_seconds)),
                    _iso(moment),
                    item_id,
                    worker_id,
                ),
            )
        return cursor.rowcount > 0

    def complete_queue_item(
        self, item_id: int, worker_id: str, *, now: datetime | None = None
    ) -> QueueItem:
        moment = _iso(now or _now())
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                UPDATE work_queue
                SET status = 'done', lease_owner = NULL, lease_expires_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (moment, item_id, worker_id),
            )
        return self.get_queue_item(item_id)

    def fail_queue_item(
        self,
        item_id: int,
        worker_id: str,
        error: str,
        *,
        retryable: bool = True,
        backoff_seconds: int = CLAIM_BACKOFF_SECONDS,
        now: datetime | None = None,
    ) -> QueueItem:
        item = self.get_queue_item(item_id)
        moment = now or _now()
        if retryable and item.attempts < item.max_attempts:
            status = "queued"
            available_at = _iso(moment + timedelta(seconds=backoff_seconds * item.attempts))
            lease_owner = None
            lease_expires_at = None
        else:
            status = "dead"
            available_at = _iso(moment)
            lease_owner = item.lease_owner
            lease_expires_at = (
                _iso(item.lease_expires_at) if item.lease_expires_at else None
            )
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                """
                UPDATE work_queue
                SET status = ?, available_at = ?, lease_owner = ?, lease_expires_at = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (
                    status,
                    available_at,
                    lease_owner,
                    lease_expires_at,
                    error,
                    _iso(moment),
                    item_id,
                    worker_id,
                ),
            )
        return self.get_queue_item(item_id)

    def dead_letter_queue(self) -> list[QueueItem]:
        return self.list_queue(status="dead")

    # Internals ------------------------------------------------------------

    def _update_run(self, run_id: str, assignments: str, params: tuple[Any, ...]) -> None:
        self._require(run_id)
        with closing(self._connect()) as conn, conn:
            self._execute(
                conn,
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, _iso(_now()), run_id),
            )

    def _update_journal(
        self,
        conn: Any,
        run_id: str,
        key: str,
        assignments: str,
        *params: Any,
    ) -> None:
        cursor = self._execute(
            conn,
            f"UPDATE journal SET {assignments}, updated_at = ? WHERE run_id = ? AND key = ?",
            (*params, _iso(_now()), run_id, key),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no journal entry '{key}' for Run {run_id}")

    def _require(self, run_id: str) -> None:
        with closing(self._connect()) as conn:
            row = self._execute(
                conn, "SELECT 1 AS present FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"no Run with id {run_id}")


def _row_to_run(row: Mapping[str, Any]) -> Run:
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


def _row_to_escalation(row: Mapping[str, Any]) -> Escalation:
    return Escalation(
        id=row["id"],
        run_id=row["run_id"],
        reason=row["reason"],
        question=row["question"],
        context=json.loads(row["context_json"]),
        resolved=bool(row["resolved"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_approval(row: Mapping[str, Any]) -> ApprovalRequest:
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


def _row_to_journal(row: Mapping[str, Any]) -> JournalEntry:
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


def _row_to_claim(row: Mapping[str, Any]) -> SideEffectClaim:
    return SideEffectClaim(
        claim_key=row["claim_key"],
        run_id=row["run_id"],
        journal_key=row["journal_key"],
        status=row["status"],
        detail=json.loads(row["detail_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_exception(row: Mapping[str, Any]) -> RunException:
    return RunException(
        id=row["id"],
        run_id=row["run_id"],
        journal_key=row["journal_key"],
        kind=row["kind"],
        summary=row["summary"],
        question=row["question"],
        evidence=json.loads(row["evidence_json"]),
        status=row["status"],
        resolution=row["resolution"],
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
    )


def _row_to_queue(row: Mapping[str, Any]) -> QueueItem:
    return QueueItem(
        id=row["id"],
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=datetime.fromisoformat(row["available_at"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=(
            datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"]
            else None
        ),
        last_error=row["last_error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


__all__ = [
    "CLAIM_BACKOFF_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "ApprovalDecisionError",
    "ApprovalNotFoundError",
    "ClaimConflictError",
    "ExceptionNotFoundError",
    "RunNotFoundError",
    "RunStore",
    "UnknownActionError",
    "generate_run_id",
]
