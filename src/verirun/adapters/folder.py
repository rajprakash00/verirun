"""The watched-folder mail source: invoice PDFs without mailbox access.

Every PDF directly inside the watched directory becomes one message with one
attachment. The bytes are copied into the document root's content-addressed
store, and the local index makes repeated syncs idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from verirun.adapters.base import (
    MailAttachment,
    MailMessage,
    NotFoundError,
)
from verirun.adapters.mailstore import (
    IndexedMailSource,
    content_hash,
    message_id_for,
    store_attachment,
)

PDF_SUFFIX = ".pdf"


class WatchedFolderSource(IndexedMailSource):
    """A directory of invoice PDFs, indexed like a mailbox."""

    def __init__(
        self, watch_dir: str | Path, document_root: str | Path, state_db: str | Path
    ) -> None:
        super().__init__(document_root, state_db)
        self.watch_dir = Path(watch_dir)
        self.source_key = f"folder:{self.watch_dir.resolve()}"

    def sync(self, folder: str = "inbox") -> int:
        """Ingest every new PDF in the watched directory; return how many."""
        if not self.watch_dir.is_dir():
            raise NotFoundError(f"watched folder not found: {self.watch_dir}")
        ingested = 0
        for path in sorted(self.watch_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix.lower() != PDF_SUFFIX:
                continue
            data = path.read_bytes()
            digest = content_hash(data)
            message_id = message_id_for(digest)
            if self.index.has_message(message_id):
                continue
            relative, digest, filename = store_attachment(
                self.document_root, data, path.name
            )
            received_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(
                timespec="seconds"
            )
            message = MailMessage(
                id=message_id,
                folder=folder,
                sender="Watched folder",
                sender_email="",
                subject=f"Invoice file {path.name}",
                body=f"Ingested from the watched folder: {path.name}",
                received_at=received_at,
                attachments=(
                    _attachment(relative, digest, filename, len(data)),
                ),
            )
            if self.index.save_message(
                message,
                source=self.source_key,
                external_id=f"file:{path.name}:{digest}",
                content_hash=digest,
            ):
                ingested += 1
        return ingested


def _attachment(
    relative: str, digest: str, filename: str, size: int
) -> MailAttachment:
    return MailAttachment(
        id=digest,
        filename=filename,
        content_type="application/pdf",
        size_bytes=size,
        path=relative,
    )
