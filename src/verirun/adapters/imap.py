"""The IMAP mail source: an AP mailbox without a vendor-specific API.

The adapter connects over IMAP with a host, port, username, and app password,
tracks the selected folder's UIDVALIDITY and last ingested UID, and copies every
message and attachment into the local index and content-addressed store. UIDs
are only meaningful inside one UIDVALIDITY epoch: when the server starts a new
epoch the adapter rescans from the first UID, and the content hash keeps an
already ingested message from being processed twice.

The local index is authoritative for the engine. Marking a message read does
not write the server's ``\\Seen`` flag; sync never depends on that flag, so a
shutdown between fetch and mark cannot lose an invoice.
"""

from __future__ import annotations

import email
import html
import imaplib
import re
from contextlib import suppress
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from verirun.adapters.base import (
    ConflictError,
    MailAttachment,
    MailMessage,
    TransientError,
)
from verirun.adapters.mailstore import (
    IndexedMailSource,
    content_hash,
    message_id_for,
    store_attachment,
)

IMAP_DEFAULT_FOLDER = "INBOX"
IMAP_TIMEOUT_S = 30.0
HTML_TAGS = re.compile(r"<[^>]+>")


class ImapSource(IndexedMailSource):
    """An AP mailbox read over IMAP and mirrored into a local index."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        document_root: str | Path,
        state_db: str | Path,
        folder: str = IMAP_DEFAULT_FOLDER,
        use_ssl: bool = True,
        timeout: float = IMAP_TIMEOUT_S,
    ) -> None:
        if not host:
            raise ValueError("an IMAP source needs a host")
        if not username:
            raise ValueError("an IMAP source needs a username")
        super().__init__(document_root, state_db)
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.folder = folder
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.source_key = f"imap:{username}@{host}:{port}/{folder}"

    def sync(self, folder: str = "inbox") -> int:
        """Fetch messages newer than the cursor; return how many were new."""
        conn = self._connect()
        try:
            uidvalidity = self._select(conn, folder)
            cursor = self.index.get_cursor(self.source_key, folder)
            same_epoch = cursor is not None and cursor.uidvalidity == uidvalidity
            start = cursor.last_uid + 1 if same_epoch else 1
            last_uid = cursor.last_uid if same_epoch else 0
            ingested = 0
            for uid in self._search(conn, start):
                raw = self._fetch(conn, uid)
                if raw is None:
                    continue
                if self._ingest(raw, folder=folder, uidvalidity=uidvalidity, uid=uid):
                    ingested += 1
                last_uid = max(last_uid, uid)
                self.index.set_cursor(self.source_key, folder, uidvalidity, last_uid)
            self.index.set_cursor(self.source_key, folder, uidvalidity, last_uid)
            return ingested
        finally:
            self._close(conn)

    def _connect(self) -> imaplib.IMAP4:
        try:
            if self.use_ssl:
                conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
            else:
                conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        except OSError as exc:
            raise TransientError(
                f"cannot connect to IMAP {self.host}:{self.port}: {exc}"
            ) from exc
        try:
            conn.login(self.username, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            with suppress(Exception):
                conn.logout()
            raise ConflictError(
                f"IMAP login failed for {self.username} at {self.host}:{self.port}: "
                f"{_safe_error(exc)}"
            ) from exc
        return conn

    def _select(self, conn: imaplib.IMAP4, folder: str) -> int:
        mailbox = self.folder if folder.lower() == "inbox" else folder
        try:
            status, _data = conn.select(mailbox, readonly=True)
            if status != "OK":
                raise ConflictError(f"IMAP cannot select mailbox {mailbox!r}")
            _kind, data = conn.response("UIDVALIDITY")
            if not data or data[0] is None:
                raise ConflictError(f"IMAP reported no UIDVALIDITY for {mailbox!r}")
            return int(data[0])
        except (imaplib.IMAP4.error, OSError) as exc:
            raise TransientError(
                f"IMAP could not select mailbox {mailbox!r}: {_safe_error(exc)}"
            ) from exc

    def _search(self, conn: imaplib.IMAP4, start: int) -> list[int]:
        try:
            status, data = conn.uid("SEARCH", None, "UID", f"{start}:*")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise TransientError(f"IMAP UID SEARCH failed: {_safe_error(exc)}") from exc
        if status != "OK":
            raise TransientError(f"IMAP UID SEARCH failed: {status}")
        raw = data[0] if data and data[0] is not None else b""
        if isinstance(raw, str):
            raw = raw.encode("ascii", "ignore")
        return [uid for uid in (int(value) for value in raw.split()) if uid >= start]

    def _fetch(self, conn: imaplib.IMAP4, uid: int) -> bytes | None:
        try:
            status, data = conn.uid(
                "FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[])"
            )
        except (imaplib.IMAP4.error, OSError) as exc:
            raise TransientError(
                f"IMAP UID FETCH {uid} failed: {_safe_error(exc)}"
            ) from exc
        if status != "OK":
            raise TransientError(f"IMAP UID FETCH {uid} failed: {status}")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1]
        return None

    def _ingest(
        self, raw: bytes, *, folder: str, uidvalidity: int, uid: int
    ) -> bool:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sender, sender_email = parseaddr(str(message.get("From", "")))
        digest = content_hash(raw)
        message_id = message_id_for(digest)
        attachments = tuple(
            self._store_part(part, index)
            for index, part in enumerate(message.iter_attachments(), start=1)
        )
        parsed = MailMessage(
            id=message_id,
            folder=folder,
            sender=sender or sender_email or "unknown sender",
            sender_email=sender_email,
            subject=str(message.get("Subject", "")).strip() or "(no subject)",
            body=_body(message),
            received_at=_received_at(message),
            attachments=attachments,
        )
        return self.index.save_message(
            parsed,
            source=self.source_key,
            external_id=f"{uidvalidity}:{uid}:{digest}",
            content_hash=digest,
            message_id=str(message.get("Message-ID", "")),
        )

    def _store_part(self, part: email.message.Message, index: int) -> MailAttachment:
        filename = part.get_filename() or f"attachment-{index}"
        data = part.get_payload(decode=True) or b""
        relative, digest, safe = store_attachment(
            self._document_root, data, filename
        )
        return MailAttachment(
            id=digest,
            filename=safe,
            content_type=part.get_content_type(),
            size_bytes=len(data),
            path=relative,
        )

    @staticmethod
    def _close(conn: imaplib.IMAP4) -> None:
        with suppress(Exception):
            conn.logout()


def _body(message: email.message.Message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (KeyError, LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        return html.unescape(HTML_TAGS.sub(" ", content)).strip()
    return content.strip()


def _received_at(message: email.message.Message) -> str:
    raw = message.get("Date")
    if raw:
        with suppress(TypeError, ValueError, OverflowError):
            moment = parsedate_to_datetime(str(raw))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(UTC).isoformat(timespec="seconds")
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_error(exc: BaseException) -> str:
    """A short error that cannot contain credentials."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
