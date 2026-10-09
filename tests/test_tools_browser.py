"""Integration tests for the browser tools against the real LedgerLite UI.

These drive headless Chromium through the tool registry exactly as the executor
will: snapshot to get stable refs, then click, type, and select by ref. No LLM
is involved.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mocks.ledgerlite import db
from tests.support import ref_for
from verirun.tools import BrowserSession, ToolRegistry, build_browser_tools

BROWSER_TOOLS = [
    "browser.navigate",
    "browser.snapshot",
    "browser.click",
    "browser.type",
    "browser.select",
    "browser.extract",
    "browser.screenshot",
]


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[BrowserSession]:
    session = BrowserSession(artifact_dir=tmp_path / "artifacts")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - report missing system dependencies as a skip
        session.close()
        pytest.skip(
            f"Chromium is unavailable ({exc}); "
            "run `uv run playwright install --with-deps chromium`"
        )
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def registry(browser: BrowserSession) -> ToolRegistry:
    return ToolRegistry(build_browser_tools(browser), allowlist=BROWSER_TOOLS)


def test_scripted_browser_sequence_files_an_invoice(
    registry: ToolRegistry, ledgerlite_server: str, ledgerlite_db: Path
) -> None:
    navigate = registry.invoke(
        "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new"}
    )
    assert navigate.ok

    form = registry.invoke("browser.snapshot")
    assert form.ok
    number = ref_for(form, role="textbox", name="Vendor invoice number")
    vendor = ref_for(form, role="combobox", name="Vendor")
    purchase_order = ref_for(form, role="combobox", name="Purchase order")
    goods_receipt = ref_for(form, role="combobox", name="Goods receipt")
    amount = ref_for(form, role="textbox", name="Amount")
    submit = ref_for(form, role="button", name="File invoice")

    assert registry.invoke("browser.type", {"ref": number, "text": "NW-2026-001"}).ok
    assert registry.invoke("browser.select", {"ref": vendor, "value": "V-1001"}).ok
    assert registry.invoke("browser.select", {"ref": purchase_order, "value": "PO-2001"}).ok
    assert registry.invoke("browser.select", {"ref": goods_receipt, "value": "GR-2501"}).ok
    assert registry.invoke("browser.type", {"ref": amount, "text": "1250.00"}).ok
    filed = registry.invoke("browser.click", {"ref": submit})
    assert filed.ok

    detail = registry.invoke("browser.snapshot")
    assert detail.ok
    assert "NW-2026-001" in detail.data["snapshot"]

    conn = db.connect(ledgerlite_db)
    try:
        row = conn.execute(
            "SELECT * FROM invoices WHERE number = 'NW-2026-001'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["vendor_id"] == "V-1001"
    assert row["po_id"] == "PO-2001"
    assert row["gr_id"] == "GR-2501"
    assert row["amount_cents"] == 125_000
    assert row["status"] == "received"


def test_snapshot_refs_click_and_type_through_two_consecutive_pages(
    registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/"}).ok

    dashboard = registry.invoke("browser.snapshot")
    assert registry.invoke(
        "browser.click", {"ref": ref_for(dashboard, role="link", name="Invoices")}
    ).ok

    invoices = registry.invoke("browser.snapshot")
    assert invoices.ok
    assert invoices.data["url"].endswith("/invoices")
    assert registry.invoke(
        "browser.click", {"ref": ref_for(invoices, role="link", name="New invoice")}
    ).ok

    form = registry.invoke("browser.snapshot")
    assert form.data["url"].endswith("/invoices/new")
    amount = ref_for(form, role="textbox", name="Amount")
    assert registry.invoke("browser.type", {"ref": amount, "text": "42.00"}).ok
    extracted = registry.invoke("browser.extract", {"ref": amount})

    assert extracted.ok
    assert extracted.data["text"] == "42.00"


def test_a_dashboard_snapshot_exposes_actions_and_content(
    registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/"}).ok

    snapshot = registry.invoke("browser.snapshot")
    headings = [node for node in snapshot.data["nodes"] if node["role"] == "heading"]

    assert snapshot.ok
    assert snapshot.data["title"].startswith("LedgerLite")
    assert any("Dashboard" in node["name"] for node in snapshot.data["nodes"])
    assert all(node["ref"] for node in snapshot.data["refs"].values())
    assert [node["name"] for node in headings] == ["Dashboard"]


def test_extract_without_a_ref_returns_the_page_text(
    registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke(
        "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new"}
    ).ok

    extracted = registry.invoke("browser.extract")

    assert extracted.ok
    assert "New invoice" in extracted.data["text"]
    assert "Vendor invoice number" in extracted.data["text"]


def test_screenshot_lands_on_disk_and_in_the_observation(
    registry: ToolRegistry, ledgerlite_server: str, tmp_path: Path
) -> None:
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/"}).ok

    shot = registry.invoke("browser.screenshot")

    assert shot.ok
    assert len(shot.artifacts) == 1
    path = Path(shot.artifacts[0])
    assert path.is_file()
    assert path.suffix == ".png"
    assert path.parent == tmp_path / "artifacts"
    assert shot.data["path"] == str(path)
    assert str(path) in shot.summary


def test_a_new_snapshot_clears_stale_refs(
    browser: BrowserSession, registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/"}).ok
    first = registry.invoke("browser.snapshot")
    invoices_ref = ref_for(first, role="link", name="Invoices")
    browser.page.evaluate(
        """(ref) => {
            const stale = document.createElement("a");
            stale.setAttribute("data-op-ref", ref);
            stale.href = "/invoices";
            stale.textContent = "stale";
            stale.style.display = "none";
            document.body.prepend(stale);
        }""",
        invoices_ref,
    )

    fresh = registry.invoke("browser.snapshot")
    clicked = registry.invoke(
        "browser.click", {"ref": ref_for(fresh, role="link", name="Invoices")}
    )

    assert clicked.ok
    assert clicked.data["url"].endswith("/invoices")


def test_a_ref_whose_control_is_gone_is_not_found(
    registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke(
        "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new"}
    ).ok
    form = registry.invoke("browser.snapshot")
    submit = ref_for(form, role="button", name="File invoice")
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/invoices"}).ok

    clicked = registry.invoke("browser.click", {"ref": submit})

    assert not clicked.ok
    assert clicked.error_kind == "not_found"


def test_select_with_an_unknown_option_is_invalid(
    registry: ToolRegistry, ledgerlite_server: str
) -> None:
    assert registry.invoke(
        "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new"}
    ).ok
    form = registry.invoke("browser.snapshot")
    vendor = ref_for(form, role="combobox", name="Vendor")

    selected = registry.invoke("browser.select", {"ref": vendor, "value": "V-9999"})

    assert not selected.ok
    assert selected.error_kind == "invalid"
    assert "V-1001" in selected.summary


def test_navigate_rejects_a_non_http_url(registry: ToolRegistry) -> None:
    observation = registry.invoke("browser.navigate", {"url": "file:///etc/passwd"})

    assert not observation.ok
    assert observation.error_kind == "invalid"


def test_unknown_ref_is_not_found(registry: ToolRegistry, ledgerlite_server: str) -> None:
    assert registry.invoke("browser.navigate", {"url": f"{ledgerlite_server}/"}).ok

    clicked = registry.invoke("browser.click", {"ref": "e999"})

    assert not clicked.ok
    assert clicked.error_kind == "not_found"
