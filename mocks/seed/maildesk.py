"""Reset and seed MailDesk's SQLite mailbox and the shared file tree.

Deterministic by construction: every id, filename, and timestamp is a literal,
and resetting deletes the database and regenerates the managed tree folders.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from mocks.maildesk import db
from mocks.seed import documents, scenarios

TREE_DIRS = ("documents", "archive", "processed")

BATCH_RECEIVED_AT = "2026-09-06T08:15:00+00:00"
ONBOARDING_RECEIVED_AT = "2026-09-06T09:05:00+00:00"
VENDOR_RECEIVED_AT = {
    "duplicate": "2026-09-05T10:02:00+00:00",
    "amount_mismatch": "2026-09-05T10:17:00+00:00",
    "missing_po": "2026-09-05T11:08:00+00:00",
    "over_limit": "2026-09-05T11:41:00+00:00",
    "forbidden_action": "2026-09-05T12:05:00+00:00",
    "transient": "2026-09-05T13:22:00+00:00",
    "scanned": "2026-09-05T14:47:00+00:00",
}

AP_MAILBOX = "ap@company.example"
PROCUREMENT_MAILBOX = "procurement@company.example"


@dataclass(frozen=True)
class AttachmentSpec:
    filename: str
    path: Path
    content_type: str = "application/pdf"


@dataclass(frozen=True)
class MessageSpec:
    id: str
    folder: str
    sender_name: str
    sender_email: str
    recipients: str
    subject: str
    body: str
    received_at: str
    scenario: str
    attachments: tuple[AttachmentSpec, ...]


def reset_and_seed(db_path: Path, shared_root: Path) -> None:
    db_path = Path(db_path)
    shared_root = Path(shared_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    reset_shared_tree(shared_root)
    with closing(db.connect(db_path)) as conn:
        db.initialize(conn)
        seed(conn, shared_root)
        conn.commit()


def reset_shared_tree(shared_root: Path) -> None:
    shared_root = Path(shared_root)
    for name in TREE_DIRS:
        directory = shared_root / name
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)


def seed(conn: sqlite3.Connection, shared_root: Path) -> None:
    documents.write_documents(shared_root)
    attachment_number = 8001
    for message in message_specs():
        conn.execute(
            """
            INSERT INTO messages
                (id, folder, sender_name, sender_email, recipients, subject, body,
                 received_at, scenario)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.folder,
                message.sender_name,
                message.sender_email,
                message.recipients,
                message.subject,
                message.body,
                message.received_at,
                message.scenario,
            ),
        )
        for attachment in message.attachments:
            path = Path(shared_root) / attachment.path
            conn.execute(
                """
                INSERT INTO attachments
                    (id, message_id, filename, content_type, size_bytes, path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ATT-{attachment_number:04d}",
                    message.id,
                    attachment.filename,
                    attachment.content_type,
                    path.stat().st_size,
                    attachment.path.as_posix(),
                ),
            )
            attachment_number += 1


def message_specs() -> tuple[MessageSpec, ...]:
    batch_scenarios = tuple(
        scenario for scenario in scenarios.SCENARIOS if scenario.key != scenarios.SCANNED.key
    )
    failure_scenarios = tuple(
        scenario for scenario in scenarios.SCENARIOS if scenario.key != scenarios.HAPPY.key
    )
    specs = [_batch_message("MSG-7001", batch_scenarios)]
    for index, scenario in enumerate(failure_scenarios):
        specs.append(_invoice_message(f"MSG-{7002 + index:04d}", scenario))
    specs.append(_onboarding_message("MSG-7009"))
    return tuple(specs)


def _invoice_attachment(scenario: scenarios.InvoiceScenario) -> AttachmentSpec:
    path = documents.invoice_relpath(scenario)
    return AttachmentSpec(filename=path.name, path=path)


def _batch_message(
    message_id: str, attachments: tuple[scenarios.InvoiceScenario, ...]
) -> MessageSpec:
    return MessageSpec(
        id=message_id,
        folder="inbox",
        sender_name="Dana Whitfield",
        sender_email="dana.whitfield@company.example",
        recipients=AP_MAILBOX,
        subject="Invoice batch for processing — September",
        body=(
            "Hi,\n\n"
            "Please process the attached supplier invoices for September. Match each "
            "one against its purchase order and goods receipt before payment, and "
            "route anything unusual to me rather than guessing.\n\n"
            "Thanks,\n"
            "Dana"
        ),
        received_at=BATCH_RECEIVED_AT,
        scenario="batch",
        attachments=tuple(_invoice_attachment(scenario) for scenario in attachments),
    )


def _invoice_message(message_id: str, scenario: scenarios.InvoiceScenario) -> MessageSpec:
    reference = (
        f" against purchase order {scenario.po_id}" if scenario.po_id is not None else ""
    )
    note = {
        "scanned": "The attached invoice is a scan of the paper original.",
        "transient": "Our supplier portal was slow this morning, so please retry if needed.",
    }.get(scenario.key)
    amount = f"{scenario.invoice_amount_cents / 100:,.2f} USD"
    lines = [
        "Hello,",
        "",
        f"Attached is invoice {scenario.invoice_number} for {amount}{reference}.",
    ]
    if note is not None:
        lines += ["", note]
    lines += [
        "",
        "Please confirm receipt and the payment terms of Net 30.",
        "",
        "Regards,",
        scenario.vendor_name,
    ]
    return MessageSpec(
        id=message_id,
        folder="inbox",
        sender_name=scenario.vendor_name,
        sender_email=scenario.vendor_email,
        recipients=AP_MAILBOX,
        subject=f"Invoice {scenario.invoice_number} from {scenario.vendor_name}",
        body="\n".join(lines),
        received_at=VENDOR_RECEIVED_AT[scenario.key],
        scenario=scenario.key,
        attachments=(_invoice_attachment(scenario),),
    )


def _onboarding_message(message_id: str) -> MessageSpec:
    vendor = scenarios.VENDOR_ONBOARDING
    return MessageSpec(
        id=message_id,
        folder="inbox",
        sender_name=vendor.contact_name,
        sender_email=vendor.contact_email,
        recipients=PROCUREMENT_MAILBOX,
        subject=f"New supplier setup — {vendor.company_name}",
        body=(
            "Hello,\n\n"
            f"Please set up {vendor.company_name} as a new supplier. Our completed "
            "W-9 is attached. I am the accounts contact on our side.\n\n"
            "Thank you,\n"
            f"{vendor.contact_name}"
        ),
        received_at=ONBOARDING_RECEIVED_AT,
        scenario=vendor.key,
        attachments=(
            AttachmentSpec(
                filename=vendor.tax_form_filename,
                path=documents.tax_form_relpath(vendor),
            ),
        ),
    )
