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
)
from verirun.adapters.memory import InMemoryAccountingSystem
from verirun.adapters.mock import MockAccountingSystem, MockMailSource

__all__ = [
    "AccountingReads",
    "AccountingSystem",
    "AdapterError",
    "Approval",
    "Bill",
    "ConflictError",
    "GoodsReceipt",
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
]
