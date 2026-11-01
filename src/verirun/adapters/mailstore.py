"""The local mail index and the content-addressed attachment store.

IMAP and the watched folder both ingest into this small SQLite index: one row
per message, one row per attachment, and a per-folder cursor. The attachment
bytes live under the document root at ``attachments/<sha-prefix>/<sha>/<name>``,
so identical content is stored once and every message links to the same object.

Message ids are derived from the content hash. Reconnecting, changing
UIDVALIDITY, or re-syncing a folder therefore cannot ingest the same message
twice.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from verirun.adapters.base import (
    ConflictError,
    MailAttachment,
    MailMessage,
    MailSource,
    MailSummary,
    NotFoundError,
)

MESSAGE_ID_PREFIX = "MSG-"
MESSAGE_ID_CHARS = 12
ATTACHMENTS_DIR = "attachments"
UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    folder TEXT NOT NULL,
    external_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    read_at TEXT,
    content_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external
    ON messages (source, folder, external_id);
CREATE INDEX IF NOT EXISTS messages_folder
    ON messages (folder, received_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (message_id, id)
);

CREATE TABLE IF NOT EXISTS cursors (
    source TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_uid INTEGER NOT NULL,
    PRIMARY KEY (source, folder)
);
"""


@dataclass(frozen=True)
class Cursor:
    """How far one IMAP folder has been ingested."""

    uidvalidity: int
    last_uid: int


class MailIndex:
    """The local record of ingested messages and their attachments."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def save_message(
        self,
        message: MailMessage,
        *,
        source: str,
        external_id: str,
        content_hash: str,
        message_id: str = "",
    ) -> bool:
        """Store one message and its attachment links; False if already known."""
        with closing(self.connect()) as conn, conn:
            existing = conn.execute(
                "SELECT id FROM messages WHERE id = ?", (message.id,)
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                """
                INSERT INTO messages
                    (id, source, folder, external_id, message_id, sender,
                     sender_email, subject, body, received_at, read_at,
                     content_hash, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    message.id,
                    source,
                    message.folder,
                    external_id,
                    message_id,
                    message.sender,
                    message.sender_email,
                    message.subject,
                    message.body,
                    message.received_at,
                    content_hash,
                    _now(),
                ),
            )
            for attachment in message.attachments:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO attachments
                        (id, message_id, filename, content_type, size_bytes, path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attachment.id,
                        message.id,
                        attachment.filename,
                        attachment.content_type,
                        attachment.size_bytes,
                        attachment.path,
                    ),
                )
        return True

    def has_message(self, message_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def list_messages(self, folder: str = "inbox") -> list[MailSummary]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT messages.*, COUNT(attachments.id) AS attachment_count
                FROM messages
                LEFT JOIN attachments ON attachments.message_id = messages.id
                WHERE messages.folder = ?
                GROUP BY messages.id
                ORDER BY messages.received_at DESC, messages.id DESC
                """,
                (folder,),
            ).fetchall()
        return [
            MailSummary(
                id=row["id"],
                folder=row["folder"],
                sender=row["sender"],
                sender_email=row["sender_email"],
                subject=row["subject"],
                received_at=row["received_at"],
                attachment_count=row["attachment_count"],
                unread=row["read_at"] is None,
            )
            for row in rows
        ]

    def read_message(self, message_id: str) -> MailMessage:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"no message '{message_id}' in the mailbox")
            rows = conn.execute(
                "SELECT * FROM attachments WHERE message_id = ? ORDER BY filename, id",
                (message_id,),
            ).fetchall()
        return MailMessage(
            id=row["id"],
            folder=row["folder"],
            sender=row["sender"],
            sender_email=row["sender_email"],
            subject=row["subject"],
            body=row["body"],
            received_at=row["received_at"],
            attachments=tuple(_attachment(item) for item in rows),
        )

    def get_attachment(self, message_id: str, attachment_id: str) -> MailAttachment:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE message_id = ? AND id = ?",
                (message_id, attachment_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"no attachment '{attachment_id}' on message '{message_id}'"
            )
        return _attachment(row)

    def mark_read(self, message_id: str) -> str:
        with closing(self.connect()) as conn, conn:
            row = conn.execute(
                "SELECT read_at FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"no message '{message_id}' in the mailbox")
            read_at = row["read_at"] or _now()
            conn.execute(
                "UPDATE messages SET read_at = ? WHERE id = ?", (read_at, message_id)
            )
        return read_at

    def get_cursor(self, source: str, folder: str) -> Cursor | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT uidvalidity, last_uid FROM cursors WHERE source = ? AND folder = ?",
                (source, folder),
            ).fetchone()
        return Cursor(uidvalidity=row["uidvalidity"], last_uid=row["last_uid"]) if row else None

    def set_cursor(self, source: str, folder: str, uidvalidity: int, last_uid: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO cursors (source, folder, uidvalidity, last_uid)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, folder) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_uid = excluded.last_uid
                """,
                (source, folder, uidvalidity, last_uid),
            )


def content_hash(data: bytes) -> str:
    return sha256(data).hexdigest()


class IndexedMailSource(MailSource):
    """A local mail index plus the content-addressed attachment store.

    IMAP and the watched folder share every read path: they differ only in how
    new messages reach the index.
    """

    def __init__(self, document_root: str | Path, state_db: str | Path) -> None:
        self._document_root = Path(document_root)
        self._document_root.mkdir(parents=True, exist_ok=True)
        self.index = MailIndex(state_db)

    @property
    def document_root(self) -> Path:
        return self._document_root

    def list_messages(self, folder: str = "inbox") -> list[MailSummary]:
        return self.index.list_messages(folder)

    def read_message(self, message_id: str) -> MailMessage:
        return self.index.read_message(message_id)

    def read_attachment(self, message_id: str, attachment_id: str) -> bytes:
        return load_attachment(
            self.index, self._document_root, message_id, attachment_id
        )

    def mark_read(self, message_id: str) -> str:
        return self.index.mark_read(message_id)


def message_id_for(digest: str) -> str:
    return f"{MESSAGE_ID_PREFIX}{digest[:MESSAGE_ID_CHARS]}"


def safe_filename(filename: str) -> str:
    """A filename safe for one path segment, keeping the readable name."""
    name = Path(filename).name.strip()
    cleaned = UNSAFE_FILENAME.sub("_", name).strip("._-")
    return (cleaned or "attachment")[:120]


def store_attachment(
    document_root: str | Path, data: bytes, filename: str
) -> tuple[str, str, str]:
    """Store bytes content-addressed; return (relative path, sha256, filename)."""
    root = Path(document_root)
    digest = content_hash(data)
    name = safe_filename(filename)
    relative = Path(ATTACHMENTS_DIR) / digest[:2] / digest[2:4] / digest / name
    target = root / relative
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return relative.as_posix(), digest, name


def resolve_attachment(document_root: str | Path, relative: str) -> Path:
    """Resolve an indexed attachment path, refusing anything outside the root."""
    root = Path(document_root).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ConflictError(f"attachment path '{relative}' escapes the document root")
    return candidate


def load_attachment(
    index: MailIndex, document_root: str | Path, message_id: str, attachment_id: str
) -> bytes:
    """Read an attachment and verify it still matches its content hash."""
    attachment = index.get_attachment(message_id, attachment_id)
    path = resolve_attachment(document_root, attachment.path)
    if not path.is_file():
        raise NotFoundError(f"attachment file not found: {attachment.path}")
    data = path.read_bytes()
    if content_hash(data) != attachment.id:
        raise ConflictError(
            f"attachment '{attachment.path}' does not match its content address"
        )
    return data


def _attachment(row: sqlite3.Row) -> MailAttachment:
    return MailAttachment(
        id=row["id"],
        filename=row["filename"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        path=row["path"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
