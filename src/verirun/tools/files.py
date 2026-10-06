"""File tools: list, read, write, move, and archive documents under one root."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, ClassVar

from pypdf import PdfReader

from verirun.engine.models import Observation
from verirun.tools.base import Tool, ToolError, optional_bool, optional_str, require_str


class FileTool(Tool):
    """A file tool rooted at one directory. Paths may never escape it."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        raw = Path(relative)
        candidate = (raw if raw.is_absolute() else self.root / raw).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise ToolError("invalid", f"path '{relative}' escapes the shared file root")
        return candidate

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    def existing(self, relative: str, *, kind: str | None = None) -> Path:
        path = self.resolve(relative)
        if not path.exists():
            raise ToolError("not_found", f"no such {kind or 'path'}: '{relative}'")
        if kind == "file" and not path.is_file():
            raise ToolError("invalid", f"'{relative}' is not a file")
        if kind == "directory" and not path.is_dir():
            raise ToolError("invalid", f"'{relative}' is not a directory")
        return path

    def ensure_replaceable(self, target: Path, overwrite: bool) -> None:
        if not target.exists():
            return
        if not overwrite:
            raise ToolError("invalid", f"'{self.relative(target)}' already exists")
        if target.is_dir():
            raise ToolError("invalid", f"cannot overwrite directory '{self.relative(target)}'")
        target.unlink()


def read_pdf_text(path: Path) -> tuple[str, int]:
    """Extract the text layer of a PDF: the joined text and the page count."""
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ToolError("invalid", f"cannot read PDF '{path.name}': {exc}") from exc
    return "\n".join(pages), len(pages)


class ListFilesTool(FileTool):
    name = "files.list"
    description = "List the files and directories inside a directory of the shared file tree."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory relative to the shared root. Defaults to the root.",
            }
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        relative = optional_str(args, "path", ".") or "."
        directory = self.existing(relative, kind="directory")
        entries = []
        for child in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name)):
            entries.append(
                {
                    "name": child.name,
                    "path": self.relative(child),
                    "kind": "directory" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return Observation(
            ok=True,
            summary=f"Listed {len(entries)} entries in '{self.relative(directory)}'",
            data={"path": self.relative(directory), "entries": entries},
        )


class ReadFileTool(FileTool):
    name = "files.read"
    description = (
        "Read a document from the shared file tree. PDFs yield their text layer; "
        "other files are read as UTF-8 text."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the shared root."}
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        relative = require_str(args, "path")
        path = self.existing(relative, kind="file")
        if path.suffix.lower() == ".pdf":
            text, pages = read_pdf_text(path)
            text_layer = bool(text.strip())
            summary = (
                f"Read {pages} page(s) of PDF text from '{relative}'"
                if text_layer
                else (
                    f"Read {pages} page(s) from '{relative}' with no text layer; "
                    "use files.extract for the vision path"
                )
            )
            return Observation(
                ok=True,
                summary=summary,
                data={
                    "path": self.relative(path),
                    "text": text,
                    "pages": pages,
                    "text_layer": text_layer,
                },
            )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("invalid", f"'{relative}' is not readable as text") from exc
        return Observation(
            ok=True,
            summary=f"Read {len(text)} characters from '{relative}'",
            data={"path": self.relative(path), "text": text},
        )


class WriteFileTool(FileTool):
    name = "files.write"
    description = "Write a text document into the shared file tree, creating directories as needed."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the shared root."},
            "content": {"type": "string", "description": "The text to write."},
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing file. Defaults to true.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        relative = require_str(args, "path")
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("invalid", "missing required argument 'content'")
        overwrite = optional_bool(args, "overwrite", True)
        path = self.resolve(relative)
        if path.is_dir():
            raise ToolError("invalid", f"'{relative}' is a directory")
        if path.exists() and not overwrite:
            raise ToolError("invalid", f"'{relative}' already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return Observation(
            ok=True,
            summary=f"Wrote {len(content.encode('utf-8'))} bytes to '{self.relative(path)}'",
            data={"path": self.relative(path), "bytes_written": len(content.encode("utf-8"))},
        )


class MoveFileTool(FileTool):
    name = "files.move"
    description = "Move or rename a file or directory inside the shared file tree."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path to move, relative to the root."},
            "destination": {
                "type": "string",
                "description": "Target path or existing directory, relative to the root.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing file. Defaults to false.",
            },
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        source = self.existing(require_str(args, "source"))
        destination = require_str(args, "destination")
        overwrite = optional_bool(args, "overwrite", False)
        target = self._target(source, destination, overwrite)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return Observation(
            ok=True,
            summary=f"Moved '{self.relative(source)}' to '{self.relative(target)}'",
            data={"source": self.relative(source), "path": self.relative(target)},
        )

    def _target(self, source: Path, destination: str, overwrite: bool) -> Path:
        candidate = self.resolve(destination)
        target = candidate / source.name if candidate.is_dir() else candidate
        if target == source:
            raise ToolError("invalid", f"'{destination}' is already the source path")
        self.ensure_replaceable(target, overwrite)
        return target


class ArchiveFileTool(FileTool):
    name = "files.archive"
    description = (
        "Archive a processed document by moving it into an archive folder, "
        "for example 'processed'."
    )
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Document to archive, relative to the root."},
            "directory": {
                "type": "string",
                "description": "Archive folder relative to the root. Defaults to 'archive'.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing archived file. Defaults to false.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        source = self.existing(require_str(args, "path"), kind="file")
        directory = optional_str(args, "directory", "archive") or "archive"
        overwrite = optional_bool(args, "overwrite", False)
        archive_root = self.resolve(directory)
        if archive_root.exists() and not archive_root.is_dir():
            raise ToolError("invalid", f"archive path '{directory}' is not a directory")
        target = archive_root / source.name
        if target == source:
            return Observation(
                ok=True,
                summary=f"'{self.relative(source)}' is already archived",
                data={"path": self.relative(source)},
            )
        self.ensure_replaceable(target, overwrite)
        archive_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return Observation(
            ok=True,
            summary=f"Archived '{self.relative(source)}' as '{self.relative(target)}'",
            data={"source": self.relative(source), "path": self.relative(target)},
        )


def build_file_tools(root: str | Path) -> list[Tool]:
    """The file tools, all rooted at one directory."""
    return [
        ListFilesTool(root),
        ReadFileTool(root),
        WriteFileTool(root),
        MoveFileTool(root),
        ArchiveFileTool(root),
    ]
