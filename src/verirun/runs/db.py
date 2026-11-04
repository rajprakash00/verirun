"""The two databases the Run store runs on: SQLite and PostgreSQL.

SQLite is the zero-dependency store for the demo and the test suite. PostgreSQL
is the product store: the worker claims queued work with ``FOR UPDATE SKIP
LOCKED`` leases instead of relying on a single process.

Both drivers expose the same tiny surface to :class:`verirun.runs.store.RunStore`:
connections returned from ``connect()`` accept ``execute`` with ``?``
placeholders (PostgreSQL translates them to ``%s``), and ``insert`` hides the
difference between ``lastrowid`` and ``RETURNING id``.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

SQLITE_PREFIXES = ("sqlite:",)
POSTGRES_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")


def is_postgres_target(target: str | Path) -> bool:
    return isinstance(target, str) and target.startswith(POSTGRES_PREFIXES)


def open_database(target: str | Path) -> Database:
    if is_postgres_target(target):
        return PostgresDatabase(str(target))
    path = Path(str(target).removeprefix("sqlite:///").removeprefix("sqlite://"))
    return SQLiteDatabase(path)


class Database(ABC):
    """A DB-API connection factory with the few differences smoothed over."""

    kind: str

    @abstractmethod
    def connect(self) -> Any:
        """A new connection; callers close it and use ``with conn`` to commit."""

    @abstractmethod
    def insert(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        """Execute one INSERT and return its generated integer id."""

    @abstractmethod
    def schema(self) -> tuple[str, ...]:
        """DDL statements that create the store's tables and indexes."""

    @abstractmethod
    def migrate(self, conn: Any) -> None:
        """Add columns introduced after the first schema to an existing store."""

    def prepare(self, sql: str) -> str:
        return sql

    def initialize(self, conn: Any) -> None:
        for statement in self.schema():
            conn.execute(statement)
        self.migrate(conn)

    def begin_claim(self, conn: Any) -> None:
        """Start the transaction a queue claim needs. SQLite needs a write lock."""


class SQLiteDatabase(Database):
    kind = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def insert(self, conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
        cursor = conn.execute(sql, params)
        return int(cursor.lastrowid)

    def begin_claim(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def schema(self) -> tuple[str, ...]:
        return tuple(
            statement.strip()
            for statement in _SCHEMA.format(serial="INTEGER PRIMARY KEY AUTOINCREMENT").split(";")
            if statement.strip()
        )

    def migrate(self, conn: sqlite3.Connection) -> None:
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


class PostgresDatabase(Database):
    kind = "postgres"

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "PostgreSQL needs the postgres extra: uv sync --extra postgres"
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        if self.dsn.startswith("postgres://"):
            self.dsn = self.dsn.replace("postgres://", "postgresql://", 1)

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def insert(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        cursor = conn.execute(f"{self.prepare(sql).rstrip()} RETURNING id", params)
        row = cursor.fetchone()
        return int(row["id"])

    def begin_claim(self, conn: Any) -> None:
        conn.execute("BEGIN")

    def prepare(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def schema(self) -> tuple[str, ...]:
        return tuple(
            statement.strip()
            for statement in _SCHEMA.format(serial="BIGSERIAL PRIMARY KEY").split(";")
            if statement.strip()
        )

    def migrate(self, conn: Any) -> None:
        # PostgreSQL supports idempotent column additions, so a store created by
        # an older release gains the columns the current code expects.
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS steps_used INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0")


_SCHEMA = """
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
)
;

CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    id TEXT NOT NULL,
    goal TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL,
    done_criterion TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (run_id, position)
)
;

CREATE TABLE IF NOT EXISTS observations (
    id {serial},
    run_id TEXT NOT NULL,
    step_position INTEGER,
    tool TEXT,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{{}}',
    error_kind TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    attempt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)
;

CREATE TABLE IF NOT EXISTS escalations (
    id {serial},
    run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    question TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{{}}',
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
;

CREATE TABLE IF NOT EXISTS approvals (
    id {serial},
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    step_position INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{{}}',
    policy TEXT,
    rule TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decision_reason TEXT,
    decided_at TEXT,
    executed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, key)
)
;

CREATE TABLE IF NOT EXISTS journal (
    id {serial},
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, key)
)
;

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    label TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, label)
)
;

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
)
;

CREATE TABLE IF NOT EXISTS evidence_packs (
    run_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
)
;

CREATE TABLE IF NOT EXISTS run_exceptions (
    id {serial},
    run_id TEXT NOT NULL,
    journal_key TEXT,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    question TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
)
;

CREATE INDEX IF NOT EXISTS run_exceptions_open
    ON run_exceptions (run_id, status)
;

CREATE UNIQUE INDEX IF NOT EXISTS run_exceptions_once
    ON run_exceptions (run_id, journal_key, kind)
    WHERE journal_key IS NOT NULL
;

CREATE TABLE IF NOT EXISTS side_effect_claims (
    claim_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    journal_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed',
    detail_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, journal_key)
)
;

CREATE TABLE IF NOT EXISTS work_queue (
    id {serial},
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
;

CREATE UNIQUE INDEX IF NOT EXISTS work_queue_one_active
    ON work_queue (run_id, kind)
    WHERE status IN ('queued', 'leased')
;

CREATE INDEX IF NOT EXISTS work_queue_claimable
    ON work_queue (status, available_at, lease_expires_at)
;
"""
