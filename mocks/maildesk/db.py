from __future__ import annotations

import sqlite3
from pathlib import Path

TABLES = ("messages", "attachments")

SCHEMA = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    folder TEXT NOT NULL CHECK (folder IN ('inbox', 'sent')),
    sender_name TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    scenario TEXT NOT NULL,
    read_at TEXT
);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    path TEXT NOT NULL,
    UNIQUE (message_id, filename)
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "read_at" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
