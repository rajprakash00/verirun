"""Mail tools: list, read, and mark messages through a ``MailSource``.

Attachments live under the source's document root; ``mail.read`` returns their
paths relative to that root so the file tools can open them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from verirun.adapters import MailSource
from verirun.engine.models import Observation
from verirun.tools.base import Tool, optional_str, require_str


class MailTool(Tool):
    """A mail tool bound to one mail source."""

    def __init__(self, source: MailSource) -> None:
        self.source = source


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
        messages = [
            message.as_dict() for message in self.adapter_call(self.source.list_messages, folder)
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
        message = self.adapter_call(self.source.read_message, message_id)
        return Observation(
            ok=True,
            summary=f"Read message '{message_id}' from {message.sender}",
            data=message.as_dict(),
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
        read_at = self.adapter_call(self.source.mark_read, message_id)
        return Observation(
            ok=True,
            summary=f"Marked message '{message_id}' read",
            data={"message_id": message_id, "read_at": read_at},
        )


def build_mail_tools(source: MailSource) -> list[Tool]:
    """The mail tools, all bound to one mail source."""
    return [
        ListMessagesTool(source),
        ReadMessageTool(source),
        MarkReadTool(source),
    ]
