"""Mail tools: list, read, and mark messages in the MailDesk mailbox.

Attachments live in the shared file tree; ``mail.read`` returns their paths
relative to the shared root so the file tools can open them.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from company_operator.engine.models import Observation
from company_operator.tools.base import Tool, ToolError, optional_str, require_str


class MailTool(Tool):
    """A mail tool bound to one mailbox database and the shared file tree."""

    def __init__(self, db_path: str | Path, shared_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.shared_root = Path(shared_root)

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise ToolError("not_found", f"mailbox database not found: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def message(self, conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise ToolError("not_found", f"no message '{message_id}' in the mailbox")
        return row

    def attachments(self, conn: sqlite3.Connection, message_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "size_bytes": row["size_bytes"],
                "path": row["path"],
            }
            for row in rows
        ]


class ListMessagesTool(MailTool):
    name = "mail.list"
    description = "List messages in a MailDesk folder, newest first, with attachment counts."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "description": "Mailbox folder to list. Defaults to 'inbox'.",
            }
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        folder = optional_str(args, "folder", "inbox") or "inbox"
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
        messages = [
            {
                "id": row["id"],
                "sender": row["sender_name"],
                "sender_email": row["sender_email"],
                "subject": row["subject"],
                "received_at": row["received_at"],
                "scenario": row["scenario"],
                "attachment_count": row["attachment_count"],
                "unread": row["read_at"] is None,
            }
            for row in rows
        ]
        unread = sum(1 for message in messages if message["unread"])
        return Observation(
            ok=True,
            summary=f"Listed {len(messages)} message(s) in '{folder}' ({unread} unread)",
            data={"folder": folder, "messages": messages},
        )


class ReadMessageTool(MailTool):
    name = "mail.read"
    description = "Read one message: its body and the shared-tree paths of its attachments."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message id, for example MSG-7001."}
        },
        "required": ["message_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        message_id = require_str(args, "message_id")
        with closing(self.connect()) as conn:
            row = self.message(conn, message_id)
            attachments = self.attachments(conn, message_id)
        return Observation(
            ok=True,
            summary=f"Read message '{message_id}' from {row['sender_name']}",
            data={
                "id": row["id"],
                "folder": row["folder"],
                "sender": row["sender_name"],
                "sender_email": row["sender_email"],
                "subject": row["subject"],
                "body": row["body"],
                "received_at": row["received_at"],
                "scenario": row["scenario"],
                "attachments": attachments,
            },
        )


class MarkReadTool(MailTool):
    name = "mail.mark_read"
    description = "Mark one message as read. Reading the same message twice is safe."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "Message id, for example MSG-7001."}
        },
        "required": ["message_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        message_id = require_str(args, "message_id")
        with closing(self.connect()) as conn, conn:
            row = self.message(conn, message_id)
            read_at = row["read_at"] or datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE messages SET read_at = ? WHERE id = ?", (read_at, message_id)
            )
        return Observation(
            ok=True,
            summary=f"Marked message '{message_id}' read",
            data={"message_id": message_id, "read_at": read_at},
        )


def build_mail_tools(db_path: str | Path, shared_root: str | Path) -> list[Tool]:
    """The mail tools, all bound to one mailbox and the shared file tree."""
    return [
        ListMessagesTool(db_path, shared_root),
        ReadMessageTool(db_path, shared_root),
        MarkReadTool(db_path, shared_root),
    ]
