"""Tests for the watched-folder mail source.

The folder source is the no-IMAP product path: a PDF dropped into a directory
becomes a message with one content-addressed attachment, and syncing again never
reprocesses it.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from tests.india_fixture import PDF_FILENAME, write_invoice_pdf
from verirun.adapters import ConflictError, NotFoundError, WatchedFolderSource


def source(tmp_path: Path) -> tuple[WatchedFolderSource, Path]:
    watch = tmp_path / "watch"
    watch.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    return (
        WatchedFolderSource(watch, shared, tmp_path / "mail-index.db"),
        shared,
    )


def test_a_pdf_in_the_watched_folder_becomes_one_message(tmp_path: Path) -> None:
    folder, shared = source(tmp_path)
    pdf = write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    digest = sha256(pdf.read_bytes()).hexdigest()

    ingested = folder.sync()
    messages = folder.list_messages()

    assert ingested == 1
    assert len(messages) == 1
    assert messages[0].attachment_count == 1
    assert messages[0].subject == f"Invoice file {PDF_FILENAME}"
    assert messages[0].unread is True
    message = folder.read_message(messages[0].id)
    attachment = message.attachments[0]
    assert attachment.filename == PDF_FILENAME
    assert attachment.id == digest
    assert attachment.size_bytes == pdf.stat().st_size
    assert attachment.path == f"attachments/{digest[:2]}/{digest[2:4]}/{digest}/{PDF_FILENAME}"
    assert folder.document_root == shared
    assert (shared / attachment.path).is_file()
    assert folder.read_attachment(message.id, attachment.id) == pdf.read_bytes()


def test_syncing_twice_does_not_reprocess_the_same_pdf(tmp_path: Path) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)

    first = folder.sync()
    second = folder.sync()

    assert first == 1
    assert second == 0
    assert len(folder.list_messages()) == 1


def test_a_new_pdf_is_ingested_beside_the_first(tmp_path: Path) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    folder.sync()
    second = write_invoice_pdf(
        tmp_path / "watch" / "another.pdf", lines=("SECOND TAX INVOICE",)
    )
    assert second.is_file()

    ingested = folder.sync()

    assert ingested == 1
    assert len(folder.list_messages()) == 2


def test_an_identical_pdf_under_another_name_is_the_same_document(
    tmp_path: Path,
) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    (tmp_path / "watch" / "copy.pdf").write_bytes(
        (tmp_path / "watch" / PDF_FILENAME).read_bytes()
    )

    ingested = folder.sync()

    assert ingested == 1
    assert len(folder.list_messages()) == 1


def test_replacing_a_file_with_new_content_is_a_new_document(
    tmp_path: Path,
) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    folder.sync()
    write_invoice_pdf(
        tmp_path / "watch" / PDF_FILENAME, lines=("REPLACEMENT TAX INVOICE",)
    )

    ingested = folder.sync()

    assert ingested == 1
    assert len(folder.list_messages()) == 2


def test_non_pdf_files_are_ignored(tmp_path: Path) -> None:
    folder, _shared = source(tmp_path)
    (tmp_path / "watch" / "notes.txt").write_text("not an invoice", encoding="utf-8")

    assert folder.sync() == 0
    assert folder.list_messages() == []


def test_mark_read_is_idempotent_and_rejects_unknown_messages(tmp_path: Path) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    folder.sync()
    message_id = folder.list_messages()[0].id

    first = folder.mark_read(message_id)
    second = folder.mark_read(message_id)

    assert first == second
    assert folder.list_messages()[0].unread is False
    with pytest.raises(NotFoundError):
        folder.mark_read("MSG-999999999999")
    with pytest.raises(NotFoundError):
        folder.read_message("MSG-999999999999")


def test_a_tampered_attachment_is_rejected(tmp_path: Path) -> None:
    folder, shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    folder.sync()
    message = folder.read_message(folder.list_messages()[0].id)
    attachment = message.attachments[0]
    (shared / attachment.path).write_bytes(b"tampered")

    with pytest.raises(ConflictError) as excinfo:
        folder.read_attachment(message.id, attachment.id)

    assert "content address" in str(excinfo.value)


def test_the_attachment_cannot_escape_the_document_root(tmp_path: Path) -> None:
    folder, _shared = source(tmp_path)
    write_invoice_pdf(tmp_path / "watch" / PDF_FILENAME)
    folder.sync()
    message = folder.read_message(folder.list_messages()[0].id)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4")
    with sqlite3.connect(tmp_path / "mail-index.db") as conn:
        conn.execute(
            "UPDATE attachments SET path = ? WHERE message_id = ?",
            ("../outside.pdf", message.id),
        )
        conn.commit()

    with pytest.raises(ConflictError):
        folder.read_attachment(message.id, message.attachments[0].id)
