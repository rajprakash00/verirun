"""Tests for the mail tools against the seeded MailDesk mock."""

from __future__ import annotations

from pathlib import Path

from company_operator.tools import ToolRegistry
from company_operator.tools.mail import build_mail_tools


def registry(db_path: Path, shared_root: Path) -> ToolRegistry:
    return ToolRegistry(
        build_mail_tools(db_path, shared_root),
        allowlist=["mail.list", "mail.read", "mail.mark_read"],
    )


def test_list_returns_the_inbox_newest_first_with_attachment_counts(
    maildesk_state,
) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    observation = tools.invoke("mail.list")

    assert observation.ok
    messages = observation.data["messages"]
    assert len(messages) == 11
    assert messages[0]["id"] == "MSG-7011"
    batch = next(message for message in messages if message["id"] == "MSG-7001")
    assert batch["subject"] == "Invoice batch for processing — September"
    assert batch["attachment_count"] == 7
    assert batch["unread"] is True


def test_list_can_filter_by_folder(maildesk_state) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    observation = tools.invoke("mail.list", {"folder": "sent"})

    assert observation.ok
    assert observation.data["messages"] == []


def test_read_returns_the_body_and_attachment_paths_in_the_shared_tree(
    maildesk_state,
) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    observation = tools.invoke("mail.read", {"message_id": "MSG-7001"})

    assert observation.ok
    assert "supplier invoices" in observation.data["body"]
    attachments = observation.data["attachments"]
    assert len(attachments) == 7
    assert all(attachment["path"].startswith("documents/invoices/") for attachment in attachments)
    assert all(
        (maildesk_state.shared_root / attachment["path"]).is_file()
        for attachment in attachments
    )


def test_reading_an_unknown_message_is_not_found(maildesk_state) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    observation = tools.invoke("mail.read", {"message_id": "MSG-9999"})

    assert not observation.ok
    assert observation.error_kind == "not_found"


def test_mark_read_flips_the_unread_flag_and_is_idempotent(maildesk_state) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    first = tools.invoke("mail.mark_read", {"message_id": "MSG-7002"})
    second = tools.invoke("mail.mark_read", {"message_id": "MSG-7002"})
    listed = tools.invoke("mail.list")

    assert first.ok
    assert second.ok
    message = next(item for item in listed.data["messages"] if item["id"] == "MSG-7002")
    assert message["unread"] is False


def test_mark_read_rejects_an_unknown_message(maildesk_state) -> None:
    tools = registry(maildesk_state.db_path, maildesk_state.shared_root)

    observation = tools.invoke("mail.mark_read", {"message_id": "MSG-9999"})

    assert not observation.ok
    assert observation.error_kind == "not_found"


def test_mutating_mail_tool_is_a_journaled_side_effect(maildesk_state) -> None:
    tools = build_mail_tools(maildesk_state.db_path, maildesk_state.shared_root)
    by_name = {tool.name: tool for tool in tools}

    assert by_name["mail.mark_read"].side_effect
    assert not by_name["mail.list"].side_effect
    assert not by_name["mail.read"].side_effect
