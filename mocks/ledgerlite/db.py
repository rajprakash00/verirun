from __future__ import annotations

import sqlite3
from pathlib import Path

TABLES = ("vendors", "purchase_orders", "goods_receipts", "invoices", "payments", "approvals")

SCHEMA = """
CREATE TABLE vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tax_id TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'blocked', 'pending')),
    created_at TEXT NOT NULL,
    scenario TEXT NOT NULL
);

CREATE TABLE purchase_orders (
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'cancelled')),
    issued_at TEXT NOT NULL,
    scenario TEXT NOT NULL
);

CREATE TABLE goods_receipts (
    id TEXT PRIMARY KEY,
    po_id TEXT NOT NULL REFERENCES purchase_orders(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL CHECK (status IN ('received', 'partial')),
    received_at TEXT NOT NULL,
    scenario TEXT NOT NULL
);

CREATE TABLE invoices (
    id TEXT PRIMARY KEY,
    number TEXT NOT NULL,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    po_id TEXT REFERENCES purchase_orders(id),
    gr_id TEXT REFERENCES goods_receipts(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL CHECK (status IN ('received', 'approved', 'paid', 'rejected')),
    received_at TEXT NOT NULL,
    scenario TEXT NOT NULL,
    UNIQUE (vendor_id, number)
);

CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'paid', 'cancelled')),
    scenario TEXT NOT NULL
);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('invoice', 'vendor', 'purchase_order')),
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL
);

CREATE TABLE failure_flags (
    key TEXT PRIMARY KEY,
    remaining INTEGER NOT NULL CHECK (remaining >= 0),
    message TEXT NOT NULL DEFAULT ''
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
