from pathlib import Path

from pypdf import PdfReader

from mocks.seed import scenarios
from mocks.seed.maildesk import reset_and_seed


def extracted_text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(path).pages)


def invoice_pdf_path(shared_root: Path, invoice_number: str) -> Path:
    return shared_root / "documents" / "invoices" / f"{invoice_number}.pdf"


def test_invoice_pdfs_carry_an_extractable_text_layer(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    reset_and_seed(tmp_path / "maildesk.db", shared_root)

    for scenario in scenarios.SCENARIOS:
        if scenario.key == scenarios.SCANNED.key:
            continue
        text = extracted_text(invoice_pdf_path(shared_root, scenario.invoice_number))
        assert scenario.vendor_name in text
        assert scenario.invoice_number in text
        assert f"{scenario.invoice_amount_cents / 100:,.2f}" in text


def test_scanned_invoice_pdf_has_no_text_layer(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    reset_and_seed(tmp_path / "maildesk.db", shared_root)

    reader = PdfReader(invoice_pdf_path(shared_root, scenarios.SCANNED.invoice_number))
    page = reader.pages[0]

    assert page.extract_text() == ""
    assert "/Font" not in page["/Resources"]
    assert len(page.images) == 1


def test_vendor_tax_form_pdf_carries_an_extractable_text_layer(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    reset_and_seed(tmp_path / "maildesk.db", shared_root)

    path = (
        shared_root
        / "documents"
        / "vendor-onboarding"
        / scenarios.VENDOR_ONBOARDING.tax_form_filename
    )
    text = extracted_text(path)

    assert scenarios.VENDOR_ONBOARDING.company_name in text
    assert scenarios.VENDOR_ONBOARDING.tax_id in text
