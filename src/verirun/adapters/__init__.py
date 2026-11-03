"""Adapters that separate the engine from mail and accounting systems."""

from verirun.adapters.base import (
    AccountingReads,
    AccountingSystem,
    AdapterError,
    Approval,
    Bill,
    ConflictError,
    GoodsReceipt,
    MailAttachment,
    MailMessage,
    MailSource,
    MailSummary,
    NotFoundError,
    Payment,
    PurchaseOrder,
    TransientError,
    UnsupportedError,
    Vendor,
    WriteOutcomeUnknownError,
)
from verirun.adapters.folder import WatchedFolderSource
from verirun.adapters.imap import ImapSource
from verirun.adapters.memory import InMemoryAccountingSystem
from verirun.adapters.mock import MockAccountingSystem, MockMailSource
from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoAccountingSystem,
    ZohoBooksClient,
    ZohoOAuthConfig,
    ZohoTokenManager,
    ZohoTokenStore,
)

__all__ = [
    "AccountingReads",
    "AccountingSystem",
    "AdapterError",
    "Approval",
    "Bill",
    "ConflictError",
    "GoodsReceipt",
    "ImapSource",
    "InMemoryAccountingSystem",
    "MailAttachment",
    "MailMessage",
    "MailSource",
    "MailSummary",
    "MockAccountingSystem",
    "MockMailSource",
    "NotFoundError",
    "Payment",
    "PurchaseOrder",
    "TransientError",
    "UnsupportedError",
    "Vendor",
    "WatchedFolderSource",
    "WriteOutcomeUnknownError",
    "ZohoAccountingReads",
    "ZohoAccountingSystem",
    "ZohoBooksClient",
    "ZohoOAuthConfig",
    "ZohoTokenManager",
    "ZohoTokenStore",
]
