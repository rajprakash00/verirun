"""The interfaces the engine and Verifier depend on.

Verirun reads mail and reads or writes an accounting system through two
adapters. The Mock Suite implements both for the demo, eval, and tests; the
in-memory accounting fake backs deterministic engine tests; the product
adapters (IMAP and Zoho Books) land behind the same interfaces.

The Verifier depends only on ``AccountingReads``, so its token can be read-only.
The executor depends on ``AccountingSystem`` because it creates purchase bills.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

AdapterErrorKind = Literal["not_found", "invalid", "transient", "unknown"]


class AdapterError(Exception):
    """A classified adapter failure with structured detail for the engine.

    ``kind`` matches the Observation error kinds the failure ladder understands.
    ``data`` carries the comparison or existing record behind a rejection.
    """

    kind: ClassVar[AdapterErrorKind] = "unknown"

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data or {}


class NotFoundError(AdapterError):
    """The requested record or system does not exist."""

    kind = "not_found"


class ConflictError(AdapterError):
    """A business rule rejected the write: a duplicate or a mismatch."""

    kind = "invalid"


class TransientError(AdapterError):
    """A temporary failure that a retry may fix."""

    kind = "transient"


class UnsupportedError(AdapterError):
    """This adapter does not provide the requested capability."""

    kind = "invalid"


@dataclass(frozen=True)
class MailAttachment:
    """One attachment. ``path`` is relative to the adapter's document root.

    Content-addressed sources use the SHA-256 of the bytes as the attachment
    ``id``; the Mock Suite keeps its seeded ids.
    """

    id: str
    filename: str
    content_type: str
    size_bytes: int
    path: str


@dataclass(frozen=True)
class MailSummary:
    """One message in a folder listing, without its body or attachments."""

    id: str
    folder: str
    sender: str
    sender_email: str
    subject: str
    received_at: str
    attachment_count: int
    unread: bool
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The tool payload: the fields the MailDesk listing always exposed."""
        return {
            "id": self.id,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "received_at": self.received_at,
            "scenario": self.scenario,
            "attachment_count": self.attachment_count,
            "unread": self.unread,
        }


@dataclass(frozen=True)
class MailMessage:
    """One message, its body, and the attachments a reader may open."""

    id: str
    folder: str
    sender: str
    sender_email: str
    subject: str
    body: str
    received_at: str
    attachments: tuple[MailAttachment, ...]
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "folder": self.folder,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "body": self.body,
            "received_at": self.received_at,
            "scenario": self.scenario,
            "attachments": [asdict(attachment) for attachment in self.attachments],
        }


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    tax_id: str
    email: str
    address: str
    status: str
    created_at: str
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    vendor_id: str
    amount_cents: int
    currency: str
    status: str
    issued_at: str
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoodsReceipt:
    id: str
    po_id: str
    amount_cents: int
    currency: str
    status: str
    received_at: str
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Bill:
    """A purchase bill in the accounting system.

    ``status`` uses the accounting system's vocabulary, normalised to the
    Mock Suite's: received, approved, paid, or rejected.
    """

    id: str
    number: str
    vendor_id: str
    po_id: str | None
    gr_id: str | None
    amount_cents: int
    currency: str
    status: str
    received_at: str
    scenario: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Payment:
    id: str
    bill_id: str
    amount_cents: int
    currency: str
    scheduled_for: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Approval:
    """A human approval recorded by an accounting system, when it has one."""

    id: str
    subject_type: str
    subject_id: str
    kind: str
    status: str
    requested_at: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MailSource(ABC):
    """List, read, and open the attachments of the mailbox under work.

    Implementations: the Mock Suite mailbox (MailDesk), IMAP, and a watched
    folder. Message and attachment ids are stable per source.
    """

    @property
    @abstractmethod
    def document_root(self) -> Path:
        """The directory attachment paths are relative to."""

    def sync(self, folder: str = "inbox") -> int:
        """Refresh the local index from the source; return the new messages.

        The Mock Suite mailbox is already materialised, so its sync is a no-op.
        IMAP and the watched folder ingest into their own index here.
        """
        return 0

    @abstractmethod
    def list_messages(self, folder: str = "inbox") -> list[MailSummary]:
        """Messages in one folder, newest first."""

    @abstractmethod
    def read_message(self, message_id: str) -> MailMessage:
        """One message with its body and attachment metadata."""

    @abstractmethod
    def read_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """The attachment's bytes, for tools that need to inspect the file."""

    @abstractmethod
    def mark_read(self, message_id: str) -> str:
        """Mark a message read and return the read timestamp. Idempotent."""


class AccountingReads(ABC):
    """Read vendors, purchase orders, and bills from the system of record.

    This is the whole surface the Verifier gets. A product integration can hand
    the Verifier a read-only token.
    """

    display_name: ClassVar[str] = "the accounting system"
    policy_system: ClassVar[str] = "accounting"

    @abstractmethod
    def list_vendors(self, *, status: str | None = None) -> list[Vendor]:
        """Vendors, optionally filtered by status, sorted by id."""

    @abstractmethod
    def get_vendor(self, vendor_id: str) -> Vendor:
        """One vendor or ``NotFoundError``."""

    @abstractmethod
    def find_vendors(
        self, *, name: str | None = None, tax_id: str | None = None
    ) -> list[Vendor]:
        """Vendors matching any supplied key (OR), sorted by id."""

    @abstractmethod
    def list_purchase_orders(
        self, *, vendor_id: str | None = None, status: str | None = None
    ) -> list[PurchaseOrder]:
        """Purchase orders matching every filter, sorted by id."""

    @abstractmethod
    def get_purchase_order(self, po_id: str) -> PurchaseOrder:
        """One purchase order or ``NotFoundError``."""

    @abstractmethod
    def get_goods_receipt(self, gr_id: str) -> GoodsReceipt:
        """One goods receipt or ``NotFoundError``."""

    @abstractmethod
    def list_bills(
        self,
        *,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
    ) -> list[Bill]:
        """Bills matching every filter, sorted by id."""

    @abstractmethod
    def get_bill(self, bill_id: str) -> Bill:
        """One bill or ``NotFoundError``."""

    @abstractmethod
    def list_payments(self, bill_id: str) -> list[Payment]:
        """Every payment recorded for one bill, sorted by id."""

    @abstractmethod
    def find_pending_approval(self, bill_id: str) -> Approval | None:
        """The pending approval for one bill, if the system records one."""


class AccountingSystem(AccountingReads, ABC):
    """The write side the executor needs: purchase bills and mock-suite extras.

    ``create_vendor`` and ``schedule_payment`` exist for the Mock Suite Task
    Packs. Product adapters inherit the default implementations, which raise
    ``UnsupportedError``, unless they need those capabilities.
    """

    @abstractmethod
    def create_purchase_bill(
        self,
        *,
        number: str,
        vendor_id: str,
        amount_cents: int,
        currency: str = "USD",
        po_id: str | None = None,
        gr_id: str | None = None,
    ) -> Bill:
        """Create one bill, rejecting duplicates and reference mismatches."""

    def create_vendor(
        self, *, name: str, tax_id: str, email: str, address: str
    ) -> Vendor:
        """Create one vendor; unsupported unless the system records vendors."""
        raise UnsupportedError(f"{self.display_name} does not create vendors")

    def schedule_payment(
        self,
        *,
        bill_id: str,
        amount_cents: int | None = None,
        scheduled_for: str | None = None,
    ) -> Payment:
        """Schedule the full payment for a bill; unsupported unless the system pays."""
        raise UnsupportedError(f"{self.display_name} does not schedule payments")
