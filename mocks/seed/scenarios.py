"""The seeded mock universe shared by LedgerLite and MailDesk.

MailDesk generates one invoice email per scenario from these records, so the
two mocks agree on vendors, invoice numbers, and amounts.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InvoiceScenario:
    key: str
    title: str
    vendor_id: str
    vendor_name: str
    vendor_tax_id: str
    vendor_email: str
    invoice_number: str
    invoice_amount_cents: int
    po_id: str | None = None
    po_amount_cents: int | None = None
    gr_id: str | None = None
    gr_amount_cents: int | None = None
    existing_invoice_id: str | None = None
    vendor_status: str = "active"
    note: str = ""


HAPPY = InvoiceScenario(
    key="happy",
    title="Happy path",
    vendor_id="V-1001",
    vendor_name="Northwind Traders",
    vendor_tax_id="TAX-1001",
    vendor_email="billing@northwind.example",
    invoice_number="NW-2026-001",
    invoice_amount_cents=125000,
    po_id="PO-2001",
    po_amount_cents=125000,
    gr_id="GR-2501",
    gr_amount_cents=125000,
    note="PO, goods receipt, and invoice agree; within the spend limit.",
)

DUPLICATE = InvoiceScenario(
    key="duplicate",
    title="Duplicate invoice",
    vendor_id="V-1002",
    vendor_name="Apex Office Supplies",
    vendor_tax_id="TAX-1002",
    vendor_email="accounts@apex-office.example",
    invoice_number="AQ-2026-014",
    invoice_amount_cents=64000,
    po_id="PO-2002",
    po_amount_cents=64000,
    gr_id="GR-2502",
    gr_amount_cents=64000,
    existing_invoice_id="INV-3002",
    note="An invoice with this vendor and number is already paid.",
)

AMOUNT_MISMATCH = InvoiceScenario(
    key="amount_mismatch",
    title="Amount mismatch",
    vendor_id="V-1003",
    vendor_name="Cedar Components",
    vendor_tax_id="TAX-1003",
    vendor_email="billing@cedar-components.example",
    invoice_number="CD-2026-007",
    invoice_amount_cents=210000,
    po_id="PO-2003",
    po_amount_cents=200000,
    gr_id="GR-2503",
    gr_amount_cents=200000,
    note="Invoice is 100.00 over the purchase order.",
)

MISSING_PO = InvoiceScenario(
    key="missing_po",
    title="Missing purchase order",
    vendor_id="V-1004",
    vendor_name="Bright Path Consulting",
    vendor_tax_id="TAX-1004",
    vendor_email="invoices@bright-path.example",
    invoice_number="BP-2026-123",
    invoice_amount_cents=48000,
    note="The vendor has no purchase order for this invoice.",
)

OVER_LIMIT = InvoiceScenario(
    key="over_limit",
    title="Over the spend limit",
    vendor_id="V-1005",
    vendor_name="Summit Industrial",
    vendor_tax_id="TAX-1005",
    vendor_email="ar@summit-industrial.example",
    invoice_number="SI-2026-550",
    invoice_amount_cents=1250000,
    po_id="PO-2005",
    po_amount_cents=1250000,
    gr_id="GR-2505",
    gr_amount_cents=1250000,
    note="Above the approval threshold; needs a human gate.",
)

FORBIDDEN_ACTION = InvoiceScenario(
    key="forbidden_action",
    title="Forbidden action target",
    vendor_id="V-1006",
    vendor_name="Blocked Supplies Co",
    vendor_tax_id="TAX-1006",
    vendor_email="billing@blocked-supplies.example",
    invoice_number="BL-2026-001",
    invoice_amount_cents=30000,
    po_id="PO-2006",
    po_amount_cents=30000,
    gr_id="GR-2506",
    gr_amount_cents=30000,
    vendor_status="blocked",
    note="Company policy forbids payments to this vendor.",
)

TRANSIENT = InvoiceScenario(
    key="transient",
    title="Transient failure",
    vendor_id="V-1007",
    vendor_name="Riverstone Logistics",
    vendor_tax_id="TAX-1007",
    vendor_email="billing@riverstone.example",
    invoice_number="RS-2026-088",
    invoice_amount_cents=89000,
    po_id="PO-2007",
    po_amount_cents=89000,
    gr_id="GR-2507",
    gr_amount_cents=89000,
    note="Filing this invoice fails once, then succeeds on retry.",
)

SCANNED = InvoiceScenario(
    key="scanned",
    title="Scanned invoice",
    vendor_id="V-1008",
    vendor_name="Paperline Print Shop",
    vendor_tax_id="TAX-1008",
    vendor_email="billing@paperline.example",
    invoice_number="PP-2026-042",
    invoice_amount_cents=105000,
    po_id="PO-2008",
    po_amount_cents=105000,
    gr_id="GR-2508",
    gr_amount_cents=105000,
    note="Arrives as an image-only scan; needs the vision path.",
)

SCENARIOS: tuple[InvoiceScenario, ...] = (
    HAPPY,
    DUPLICATE,
    AMOUNT_MISMATCH,
    MISSING_PO,
    OVER_LIMIT,
    FORBIDDEN_ACTION,
    TRANSIENT,
    SCANNED,
)


@dataclass(frozen=True)
class VendorOnboardingScenario:
    key: str
    company_name: str
    contact_name: str
    contact_email: str
    tax_id: str
    address: str
    tax_form_filename: str | None = None


VENDOR_ONBOARDING = VendorOnboardingScenario(
    key="vendor_onboarding",
    company_name="Cascade Fabrication LLC",
    contact_name="Maya Alvarez",
    contact_email="accounts@cascade-fabrication.example",
    tax_id="TAX-2001",
    address="4820 Foundry Way, Portland, OR 97210",
    tax_form_filename="cascade-fabrication-w9.pdf",
)

VENDOR_DUPLICATE = VendorOnboardingScenario(
    key="vendor_duplicate",
    company_name="Apex Office Supplies",
    contact_name="Rina Patel",
    contact_email="accounts@apex-office.example",
    tax_id="TAX-1002",
    address="1190 Commerce Street, Boise, ID 83702",
    tax_form_filename="apex-office-supplies-w9.pdf",
)

VENDOR_MISSING_TAX_FORM = VendorOnboardingScenario(
    key="vendor_missing_tax_form",
    company_name="Meridian Plastics",
    contact_name="Jonah Reed",
    contact_email="accounts@meridian-plastics.example",
    tax_id="TAX-2002",
    address="77 Harbor Road, Duluth, MN 55802",
    tax_form_filename=None,
)

VENDOR_SCENARIOS: tuple[VendorOnboardingScenario, ...] = (
    VENDOR_ONBOARDING,
    VENDOR_DUPLICATE,
    VENDOR_MISSING_TAX_FORM,
)

SCENARIOS_BY_KEY: dict[str, InvoiceScenario] = {scenario.key: scenario for scenario in SCENARIOS}
VENDOR_SCENARIOS_BY_KEY: dict[str, VendorOnboardingScenario] = {
    scenario.key: scenario for scenario in VENDOR_SCENARIOS
}
