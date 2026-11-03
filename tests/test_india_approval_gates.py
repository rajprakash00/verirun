"""The India bill Approval Gates: amount, vendor, purchase order, confidence.

Each test drives the real ToolRegistry and PolicyGate with the shipped India
policy, so the facts come from the Tool and the Run's observations exactly as
they do in a live Run. An inactive vendor is a denial, not a gate.
"""

from __future__ import annotations

from pathlib import Path

from tests.support import ROOT
from verirun.adapters import InMemoryAccountingSystem, PurchaseOrder, Vendor
from verirun.context.company import load_company_context
from verirun.context.policies import PolicySet
from verirun.context.task_pack import load_task_pack
from verirun.engine.models import Observation
from verirun.runs.store import RunStore
from verirun.runtime import run_policy_facts
from verirun.tools import PolicyGate, ToolRegistry
from verirun.tools.erp import build_erp_tools

GSTIN = "29AABCV1234C1ZQ"
INVOICE_NUMBER = "PTS/2026/0142"
PO_ID = "PO-IN-2001"
CREATED_AT = "2026-09-01T09:00:00+00:00"


def gate_registry(
    tmp_path: Path,
    *,
    vendor_status: str = "active",
    min_confidence: float = 0.95,
    low_confidence: bool = False,
    purchase_order: bool = True,
) -> ToolRegistry:
    accounting = InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id="V-IN-1001",
                name="Paper Trail Supplies Pvt Ltd",
                tax_id=GSTIN,
                email="accounts@papertrail.example",
                address="14 Market Road, Bengaluru",
                status=vendor_status,
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                id=PO_ID,
                vendor_id="V-IN-1001",
                amount_cents=1_000_000,
                currency="INR",
                status="open",
                issued_at=CREATED_AT,
            )
        ]
        if purchase_order
        else [],
    )
    store = RunStore(tmp_path / "runs.db")
    store.create_run("Process the tax invoice", "invoice-processing-in", run_id="RUN-1")
    store.add_observation(
        "RUN-1",
        Observation(
            ok=True,
            summary="Extracted the invoice",
            data={
                "parsed": {
                    "supplier_name": "Paper Trail Supplies Pvt Ltd",
                    "supplier_gstin": GSTIN,
                    "invoice_number": INVOICE_NUMBER,
                    "total_paise": 1_000_000,
                    "currency": "INR",
                    "po_reference": PO_ID if purchase_order else None,
                },
                "min_confidence": min_confidence,
                "low_confidence": low_confidence,
            },
        ),
        tool="invoice.extract",
    )
    context = load_company_context(ROOT / "company")
    pack = load_task_pack(ROOT / "tasks" / "invoice-processing-in.yaml")
    return ToolRegistry(
        build_erp_tools(accounting, default_currency="INR"),
        allowlist=["erp.file_invoice"],
        gate=PolicyGate(
            context.policies,
            run_facts=run_policy_facts(store, "RUN-1", pack),
            policy_ids=pack.policies,
        ),
    )


def file_args(*, amount: float = 100.0, po_id: str | None = PO_ID) -> dict:
    arguments = {
        "number": INVOICE_NUMBER,
        "vendor_id": "V-IN-1001",
        "amount": amount,
        "currency": "INR",
    }
    if po_id:
        arguments["po_id"] = po_id
    return arguments


def test_an_amount_below_the_threshold_with_a_matched_vendor_is_allowed(
    tmp_path: Path,
) -> None:
    observation = gate_registry(tmp_path).invoke(
        "erp.file_invoice", file_args(amount=10_000.0)
    )

    assert observation.ok
    assert observation.data["invoice_id"]


def test_an_amount_above_the_threshold_parks_for_approval(tmp_path: Path) -> None:
    observation = gate_registry(tmp_path).invoke(
        "erp.file_invoice", file_args(amount=10_001.0)
    )

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert observation.data["approval_required"] is True
    assert observation.data["rule"] == "bill-approval-threshold"


def test_an_unmatched_vendor_parks_for_approval(tmp_path: Path) -> None:
    observation = gate_registry(tmp_path).invoke(
        "erp.file_invoice", {**file_args(), "vendor_id": "V-NOT-IN-ZOHO"}
    )

    assert not observation.ok
    assert observation.data["approval_required"] is True
    assert observation.data["rule"] == "unmatched-vendor-needs-approval"


def test_a_missing_required_purchase_order_parks_for_approval(
    tmp_path: Path,
) -> None:
    observation = gate_registry(tmp_path).invoke(
        "erp.file_invoice", file_args(po_id=None)
    )

    assert not observation.ok
    assert observation.data["approval_required"] is True
    assert observation.data["rule"] == "missing-purchase-order-needs-approval"


def test_a_low_confidence_extraction_parks_for_approval(tmp_path: Path) -> None:
    observation = gate_registry(
        tmp_path, min_confidence=0.42, low_confidence=True
    ).invoke("erp.file_invoice", file_args())

    assert not observation.ok
    assert observation.data["approval_required"] is True
    assert observation.data["rule"] == "low-confidence-extraction-needs-approval"


def test_an_inactive_vendor_is_a_denial_not_a_gate(tmp_path: Path) -> None:
    observation = gate_registry(tmp_path, vendor_status="inactive").invoke(
        "erp.file_invoice", file_args()
    )

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert observation.data["outcome"] == "forbid"
    assert observation.data["approval_required"] is False
    assert observation.data["rule"] == "no-bill-for-inactive-vendor"


def test_the_policy_set_is_scoped_to_the_task_pack(tmp_path: Path) -> None:
    context = load_company_context(ROOT / "company")
    pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")

    scoped = context.policies.subset(pack.policies)

    assert "spend-limits" in scoped
    assert "india-ap" not in scoped
    assert isinstance(context.policies, PolicySet)
