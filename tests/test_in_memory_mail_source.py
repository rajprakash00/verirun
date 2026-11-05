"""The in-memory mail fake lists, reads, and marks one message."""

from __future__ import annotations

from pathlib import Path

import pytest

from verirun.adapters import (
    InMemoryMailSource,
    MailAttachment,
    MailMessage,
    NotFoundError,
)

CONTENT = b"%PDF-1.4\n% fake invoice\n"
PATH = "documents/invoices/PTS-2026-0142.pdf"


def mail_source(tmp_path: Path) -> InMemoryMailSource:
    document_root = tmp_path / "shared"
    (document_root / "documents" / "invoices").mkdir(parents=True)
    (document_root / PATH).write_bytes(CONTENT)
    message = MailMessage(
        id="MSG-IN-1",
        folder="inbox",
        sender="Paper Trail Accounts",
        sender_email="accounts@papertrail.example",
        subject="Tax invoice PTS/2026/0142",
        body="Please process the attached tax invoice.",
        received_at="2026-09-05T09:00:00+00:00",
        attachments=(
            MailAttachment(
                id="attachment-1",
                filename="PTS-2026-0142.pdf",
                content_type="application/pdf",
                size_bytes=len(CONTENT),
                path=PATH,
            ),
        ),
        scenario="india_happy",
    )
    return InMemoryMailSource(document_root, [message])


def test_the_fake_lists_reads_and_marks_a_message(tmp_path: Path) -> None:
    source = mail_source(tmp_path)

    summaries = source.list_messages()

    assert [summary.id for summary in summaries] == ["MSG-IN-1"]
    assert summaries[0].unread is True
    assert summaries[0].attachment_count == 1
    assert summaries[0].scenario == "india_happy"
    message = source.read_message("MSG-IN-1")
    assert message.attachments[0].path == PATH
    assert source.read_attachment("MSG-IN-1", "attachment-1") == CONTENT

    read_at = source.mark_read("MSG-IN-1")

    assert source.list_messages()[0].unread is False
    assert source.mark_read("MSG-IN-1") == read_at


def test_the_fake_reports_missing_messages_and_attachments(tmp_path: Path) -> None:
    source = mail_source(tmp_path)

    with pytest.raises(NotFoundError):
        source.read_message("MSG-MISSING")
    with pytest.raises(NotFoundError):
        source.read_attachment("MSG-IN-1", "attachment-missing")


def test_the_fake_reports_an_attachment_file_that_is_not_on_disk(
    tmp_path: Path,
) -> None:
    source = mail_source(tmp_path)
    (tmp_path / "shared" / PATH).unlink()

    with pytest.raises(NotFoundError):
        source.read_attachment("MSG-IN-1", "attachment-1")
