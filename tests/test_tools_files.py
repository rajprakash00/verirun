"""Unit tests for the file tools: list, read, write, move, archive."""

from __future__ import annotations

from pathlib import Path

import pytest
from fpdf import FPDF

from verirun.tools import Tool, build_file_tools


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, Tool]:
    root = tmp_path / "shared"
    root.mkdir()
    return {tool.name: tool for tool in build_file_tools(root)}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "shared"


def test_all_five_file_tools_are_registered(tools: dict[str, Tool]) -> None:
    assert set(tools) == {
        "files.list",
        "files.read",
        "files.write",
        "files.move",
        "files.archive",
    }


def test_write_then_read_round_trip(tools: dict[str, Tool]) -> None:
    write = tools["files.write"].invoke({"path": "documents/note.txt", "content": "hello"})
    read = tools["files.read"].invoke({"path": "documents/note.txt"})

    assert write.ok
    assert write.data["path"] == "documents/note.txt"
    assert read.ok
    assert read.data["text"] == "hello"


def test_list_reports_files_and_directories(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/a.txt", "content": "a"})
    tools["files.write"].invoke({"path": "documents/b.txt", "content": "b"})

    listing = tools["files.list"].invoke({"path": "documents"})

    assert listing.ok
    entries = {entry["name"]: entry for entry in listing.data["entries"]}
    assert set(entries) == {"a.txt", "b.txt"}
    assert entries["a.txt"]["kind"] == "file"
    assert entries["a.txt"]["size"] == 1
    assert all("path" in entry for entry in entries.values())


def test_read_missing_file_is_not_found(tools: dict[str, Tool]) -> None:
    read = tools["files.read"].invoke({"path": "documents/nope.txt"})

    assert not read.ok
    assert read.error_kind == "not_found"


def test_reading_a_directory_is_invalid(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/a.txt", "content": "a"})

    read = tools["files.read"].invoke({"path": "documents"})

    assert not read.ok
    assert read.error_kind == "invalid"


def test_paths_outside_the_root_are_rejected(root: Path, tools: dict[str, Tool]) -> None:
    write = tools["files.write"].invoke({"path": "../escape.txt", "content": "nope"})
    read = tools["files.read"].invoke({"path": "/etc/hostname"})

    assert not write.ok
    assert write.error_kind == "invalid"
    assert not read.ok
    assert read.error_kind == "invalid"
    assert not (root.parent / "escape.txt").exists()


def test_write_refuses_to_overwrite_unless_asked(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/a.txt", "content": "first"})

    refused = tools["files.write"].invoke(
        {"path": "documents/a.txt", "content": "second", "overwrite": False}
    )
    overwritten = tools["files.write"].invoke(
        {"path": "documents/a.txt", "content": "second", "overwrite": True}
    )

    assert not refused.ok
    assert refused.error_kind == "invalid"
    assert overwritten.ok
    assert tools["files.read"].invoke({"path": "documents/a.txt"}).data["text"] == "second"


def test_move_renames_a_file(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/a.txt", "content": "a"})

    moved = tools["files.move"].invoke(
        {"source": "documents/a.txt", "destination": "archive/a.txt"}
    )

    assert moved.ok
    assert moved.data["path"] == "archive/a.txt"
    assert not tools["files.read"].invoke({"path": "documents/a.txt"}).ok
    assert tools["files.read"].invoke({"path": "archive/a.txt"}).ok


def test_move_into_an_existing_directory_keeps_the_filename(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/a.txt", "content": "a"})
    tools["files.write"].invoke({"path": "archive/.keep", "content": ""})

    moved = tools["files.move"].invoke({"source": "documents/a.txt", "destination": "archive"})

    assert moved.ok
    assert moved.data["path"] == "archive/a.txt"


def test_move_missing_source_is_not_found(tools: dict[str, Tool]) -> None:
    moved = tools["files.move"].invoke(
        {"source": "documents/nope.txt", "destination": "archive/nope.txt"}
    )

    assert not moved.ok
    assert moved.error_kind == "not_found"


def test_archive_moves_a_document_into_the_processed_folder(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/invoice.txt", "content": "invoice"})

    archived = tools["files.archive"].invoke(
        {"path": "documents/invoice.txt", "directory": "processed"}
    )

    assert archived.ok
    assert archived.data["path"] == "processed/invoice.txt"
    assert tools["files.read"].invoke({"path": "processed/invoice.txt"}).data["text"] == "invoice"


def test_archive_is_idempotent_when_already_archived(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/invoice.txt", "content": "invoice"})

    first = tools["files.archive"].invoke(
        {"path": "documents/invoice.txt", "directory": "processed"}
    )
    second = tools["files.archive"].invoke(
        {"path": "processed/invoice.txt", "directory": "processed"}
    )

    assert first.ok
    assert second.ok
    assert second.data["path"] == "processed/invoice.txt"


def test_archive_does_not_clobber_without_overwrite(tools: dict[str, Tool]) -> None:
    tools["files.write"].invoke({"path": "documents/invoice.txt", "content": "one"})
    tools["files.write"].invoke({"path": "processed/invoice.txt", "content": "two"})

    archived = tools["files.archive"].invoke(
        {"path": "documents/invoice.txt", "directory": "processed"}
    )

    assert not archived.ok
    assert archived.error_kind == "invalid"
    assert tools["files.read"].invoke({"path": "processed/invoice.txt"}).data["text"] == "two"


def test_read_extracts_text_from_a_pdf(root: Path, tools: dict[str, Tool]) -> None:
    (root / "documents").mkdir()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 10, "Invoice NW-2026-001 total 1250.00")
    pdf.output(str(root / "documents/invoice.pdf"))

    read = tools["files.read"].invoke({"path": "documents/invoice.pdf"})

    assert read.ok
    assert "NW-2026-001" in read.data["text"]
    assert read.data["pages"] == 1
    assert read.data["text_layer"] is True


def test_read_reports_a_scanned_pdf_without_a_text_layer(maildesk_state) -> None:
    tools = {tool.name: tool for tool in build_file_tools(maildesk_state.shared_root)}

    read = tools["files.read"].invoke({"path": "documents/invoices/PP-2026-042.pdf"})

    assert read.ok
    assert read.data["text"] == ""
    assert read.data["text_layer"] is False
    assert "files.extract" in read.summary


def test_missing_argument_is_invalid(tools: dict[str, Tool]) -> None:
    observation = tools["files.write"].invoke({"path": "documents/a.txt"})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "content" in observation.summary
