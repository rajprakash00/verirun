"""Tests for the IMAP mail source against a real IMAP conversation.

The fixture mailbox is a directory of .eml files served by a small in-process
IMAP4 server, so the adapter exercises ``imaplib`` and the protocol rather than
a mocked client. Reconnects, UID tracking, and UIDVALIDITY changes are real
server behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from hashlib import sha256
from pathlib import Path

import pytest

from tests.imap_server import ImapTestServer
from tests.india_fixture import PDF_FILENAME, write_email, write_invoice_pdf
from tests.support import free_port
from verirun.adapters import (
    ConflictError,
    ImapSource,
    NotFoundError,
    TransientError,
)

USERNAME = "ap@company.example"
PASSWORD = "app-password"


def make_source(
    server: ImapTestServer,
    tmp_path: Path,
    *,
    password: str = PASSWORD,
) -> tuple[ImapSource, Path]:
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    return (
        ImapSource(
            host=server.host,
            port=server.port,
            username=USERNAME,
            password=password,
            document_root=shared,
            state_db=tmp_path / "mail-index.db",
            use_ssl=False,
        ),
        shared,
    )


def fixture_eml(tmp_path: Path, name: str = "01.eml", **email: str) -> Path:
    pdf = write_invoice_pdf(tmp_path / "source" / name.replace(".eml", ".pdf"))
    return write_email(tmp_path / "mailbox" / name, pdf.read_bytes(), **email)


def test_imap_source_connects_ingests_and_reads_the_fixture_mailbox(
    tmp_path: Path,
) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        source, shared = make_source(server, tmp_path)

        ingested = source.sync()
        messages = source.list_messages()

        assert ingested == 1
        assert server.logins == [(USERNAME, PASSWORD)]
        assert len(messages) == 1
        summary = messages[0]
        assert summary.subject.startswith("Tax invoice PTS/2026/0142")
        assert summary.sender == "Paper Trail Accounts"
        assert summary.sender_email == "accounts@papertrail.example"
        assert summary.received_at.startswith("2026-09-05")
        assert summary.attachment_count == 1
        assert summary.unread is True

        message = source.read_message(summary.id)
        attachment = message.attachments[0]
        digest = sha256(attachment_bytes(tmp_path, eml)).hexdigest()
        assert attachment.id == digest
        assert attachment.filename == PDF_FILENAME
        assert attachment.path == f"attachments/{digest[:2]}/{digest[2:4]}/{digest}/{PDF_FILENAME}"
        assert (shared / attachment.path).is_file()
        assert source.read_attachment(message.id, attachment.id) == attachment_bytes(
            tmp_path, eml
        )
        assert "Please process the attached tax invoice" in message.body


def attachment_bytes(tmp_path: Path, eml: Path) -> bytes:
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(eml.read_bytes())
    part = next(message.iter_attachments())
    return part.get_payload(decode=True)


def test_reconnecting_does_not_reprocess_ingested_messages(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        source, _shared = make_source(server, tmp_path)
        source.sync()

        again = source.sync()

        assert again == 0
        assert len(source.list_messages()) == 1
        assert len(server.logins) == 2
        assert server.searches[-1] == "2:*"
        assert server.fetches == [1]


def test_a_new_message_is_ingested_from_the_next_uid(tmp_path: Path) -> None:
    first = fixture_eml(tmp_path)
    second_pdf = write_invoice_pdf(
        tmp_path / "source" / "second.pdf", lines=("SECOND TAX INVOICE", "Total: 500.00 INR")
    )
    second = write_email(
        tmp_path / "mailbox" / "02.eml",
        second_pdf.read_bytes(),
        subject="Second invoice",
        message_id="<second@papertrail.example>",
    )
    with ImapTestServer() as server:
        server.add_message(first)
        source, _shared = make_source(server, tmp_path)
        source.sync()
        server.add_message(second)

        ingested = source.sync()

        assert ingested == 1
        assert len(source.list_messages()) == 2
        assert server.searches == ["1:*", "2:*"]


def test_a_changed_uidvalidity_rescans_without_duplicating_the_old_message(
    tmp_path: Path,
) -> None:
    first = fixture_eml(tmp_path)
    second_pdf = write_invoice_pdf(
        tmp_path / "source" / "second.pdf", lines=("SECOND TAX INVOICE", "Total: 500.00 INR")
    )
    second = write_email(
        tmp_path / "mailbox" / "02.eml",
        second_pdf.read_bytes(),
        subject="Second invoice",
        message_id="<second@papertrail.example>",
    )
    with ImapTestServer() as server:
        server.add_message(first)
        source, _shared = make_source(server, tmp_path)
        source.sync()
        server.add_message(second)
        server.bump_uidvalidity()

        ingested = source.sync()

        assert ingested == 1
        assert len(source.list_messages()) == 2
        assert server.searches == ["1:*", "1:*"]


def test_bad_credentials_are_a_classified_failure_without_the_password(
    tmp_path: Path,
) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        source, _shared = make_source(server, tmp_path, password="wrong-secret")

        with pytest.raises(ConflictError) as excinfo:
            source.sync()

        assert "IMAP" in str(excinfo.value)
        assert "wrong-secret" not in str(excinfo.value)
        assert source.list_messages() == []


def test_mark_read_and_unknown_ids(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        source, _shared = make_source(server, tmp_path)
        source.sync()
        message_id = source.list_messages()[0].id

        first = source.mark_read(message_id)
        second = source.mark_read(message_id)

        assert first == second
        assert source.list_messages()[0].unread is False
        with pytest.raises(NotFoundError):
            source.read_message("MSG-999999999999")
        with pytest.raises(NotFoundError):
            source.mark_read("MSG-999999999999")


def test_imap_source_requires_a_host_and_username(tmp_path: Path) -> None:
    base = {
        "port": 993,
        "password": PASSWORD,
        "document_root": tmp_path / "shared",
        "state_db": tmp_path / "mail-index.db",
    }
    with pytest.raises(ValueError, match="host"):
        ImapSource(host="", username=USERNAME, **base)
    with pytest.raises(ValueError, match="username"):
        ImapSource(host="imap.example.test", username="", **base)


def test_a_refused_connection_is_a_transient_failure(tmp_path: Path) -> None:
    source = ImapSource(
        host="127.0.0.1",
        port=free_port(),
        username=USERNAME,
        password=PASSWORD,
        document_root=tmp_path / "shared",
        state_db=tmp_path / "mail-index.db",
        use_ssl=False,
        timeout=1.0,
    )

    with pytest.raises(TransientError, match="cannot connect"):
        source.sync()


def test_select_failure_is_a_classified_failure(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        server.fail_select = True
        source, _shared = make_source(server, tmp_path)

        with pytest.raises(ConflictError, match="select"):
            source.sync()


def test_a_server_without_uidvalidity_is_rejected(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        server.send_uidvalidity = False
        source, _shared = make_source(server, tmp_path)

        with pytest.raises(ConflictError, match="UIDVALIDITY"):
            source.sync()


def test_search_failure_is_transient(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        server.fail_search = True
        source, _shared = make_source(server, tmp_path)

        with pytest.raises(TransientError, match="SEARCH"):
            source.sync()


def test_fetch_failure_is_transient(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        server.fail_fetch = True
        source, _shared = make_source(server, tmp_path)

        with pytest.raises(TransientError, match="FETCH"):
            source.sync()


def test_a_message_deleted_before_fetch_advances_the_cursor(tmp_path: Path) -> None:
    eml = fixture_eml(tmp_path)
    with ImapTestServer() as server:
        server.add_message(eml)
        server.hide_fetch_uids = {1}
        source, _shared = make_source(server, tmp_path)

        first = source.sync()
        second = source.sync()

        assert first == 0
        assert second == 0
        assert source.list_messages() == []
        assert server.searches == ["1:*", "2:*"]


def test_an_html_only_message_gets_a_text_body_and_a_fallback_date(
    tmp_path: Path,
) -> None:
    message = EmailMessage()
    message["From"] = "HTML Supplier <billing@html.example>"
    message["To"] = USERNAME
    message["Subject"] = "HTML invoice"
    message["Message-ID"] = "<html@example>"
    message.set_content("<p>Hello <b>AP</b> team</p>", subtype="html")
    with ImapTestServer() as server:
        server.add_message(message.as_bytes())
        source, _shared = make_source(server, tmp_path)
        source.sync()

        parsed = source.read_message(source.list_messages()[0].id)

        assert "Hello" in parsed.body
        assert "AP" in parsed.body
        assert "<p>" not in parsed.body
        assert parsed.received_at.startswith(datetime.now(UTC).date().isoformat())


def test_a_message_without_a_text_part_has_an_empty_body(tmp_path: Path) -> None:
    message = EmailMessage()
    message["From"] = "Attachments Only <billing@example.test>"
    message["To"] = USERNAME
    message["Subject"] = "Invoice with no body"
    message["Date"] = "Sat, 05 Sep 2026 09:00:00 +0000"
    message["Message-ID"] = "<no-body@example>"
    message.add_attachment(
        b"%PDF-1.4\n", maintype="application", subtype="pdf", filename="only.pdf"
    )
    with ImapTestServer() as server:
        server.add_message(message.as_bytes())
        source, _shared = make_source(server, tmp_path)
        source.sync()

        parsed = source.read_message(source.list_messages()[0].id)

        assert parsed.body == ""
        assert [attachment.filename for attachment in parsed.attachments] == ["only.pdf"]
