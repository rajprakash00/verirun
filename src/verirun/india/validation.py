"""The India GST rules: one GSTIN check digit and the tax arithmetic.

These run on the extracted values, not on the document text, so the Verifier
and the Evidence Pack can show the same verdicts without trusting the model.
"""

from __future__ import annotations

import re
import string
from dataclasses import asdict, dataclass
from typing import Any

# GSTIN: state code, PAN, entity number, the literal Z, and a check digit.
# State codes 01-38 are the states and union territories; 97 is Other
# Territory and 99 is Centre Jurisdiction.
GSTIN_PATTERN = re.compile(
    r"^(?:0[1-9]|[12][0-9]|3[0-8]|97|99)"
    r"[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
)
CHECKSUM_ALPHABET = string.digits + string.ascii_uppercase


@dataclass(frozen=True)
class Validation:
    """One validation result, ready for an Observation and the Evidence Pack."""

    id: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def gstin_check_digit(first_fourteen: str) -> str:
    """The GSTIN check digit for the first fourteen characters.

    Every character is a base-36 digit. Positions alternate a weight of one and
    two; each weighted value is folded into its base-36 digits and summed. The
    check digit makes the whole sum a multiple of 36.
    """
    total = 0
    for position, character in enumerate(first_fourteen):
        value = CHECKSUM_ALPHABET.index(character)
        if position % 2 == 1:
            value *= 2
        total += value // 36 + value % 36
    return CHECKSUM_ALPHABET[(36 - total % 36) % 36]


def validate_gstin(gstin: str) -> Validation:
    """Check the format and the check digit of one GSTIN."""
    normalised = "".join(gstin.split()).upper()
    if len(normalised) != 15 or not GSTIN_PATTERN.match(normalised):
        return Validation(
            id="gstin_checksum",
            ok=False,
            detail=f"not a 15-character GSTIN with a valid format: {normalised!r}",
        )
    expected = gstin_check_digit(normalised[:14])
    actual = normalised[14]
    if actual != expected:
        return Validation(
            id="gstin_checksum",
            ok=False,
            detail=(
                f"GSTIN {normalised} has check digit {actual!r} but "
                f"{expected!r} is required"
            ),
        )
    return Validation(
        id="gstin_checksum",
        ok=True,
        detail=f"GSTIN {normalised} is well formed and its check digit is valid",
    )


def validate_tax(
    *,
    taxable_paise: int,
    cgst_paise: int | None,
    sgst_paise: int | None,
    igst_paise: int | None,
    total_paise: int,
) -> list[Validation]:
    """Check that the tax components sum to the total and form a valid split."""
    component_total = (
        taxable_paise + (cgst_paise or 0) + (sgst_paise or 0) + (igst_paise or 0)
    )
    components = (
        f"taxable {_inr(taxable_paise)} + CGST {_inr(cgst_paise or 0)} + "
        f"SGST {_inr(sgst_paise or 0)} + IGST {_inr(igst_paise or 0)}"
    )
    tax_sum = Validation(
        id="tax_sum",
        ok=component_total == total_paise,
        detail=(
            f"{components} = {_inr(component_total)}; invoice total is "
            f"{_inr(total_paise)}"
        ),
    )
    return [tax_sum, _tax_split(cgst_paise, sgst_paise, igst_paise)]


def _tax_split(
    cgst_paise: int | None, sgst_paise: int | None, igst_paise: int | None
) -> Validation:
    """CGST and SGST travel together and equal; IGST travels alone."""
    intra = cgst_paise is not None or sgst_paise is not None
    if igst_paise is not None and igst_paise > 0 and intra:
        return Validation(
            id="tax_split",
            ok=False,
            detail=(
                "an invoice cannot carry IGST together with CGST or SGST: "
                f"IGST {_inr(igst_paise)}, CGST {_inr(cgst_paise or 0)}, "
                f"SGST {_inr(sgst_paise or 0)}"
            ),
        )
    if not intra:
        if igst_paise is None:
            return Validation(
                id="tax_split", ok=True, detail="the invoice carries no GST components"
            )
        return Validation(
            id="tax_split", ok=True, detail=f"IGST {_inr(igst_paise)} only"
        )
    if cgst_paise is None or sgst_paise is None:
        missing = "SGST" if sgst_paise is None else "CGST"
        return Validation(
            id="tax_split",
            ok=False,
            detail=f"{missing} is missing; CGST and SGST must be present together",
        )
    if cgst_paise != sgst_paise:
        return Validation(
            id="tax_split",
            ok=False,
            detail=(
                f"CGST {_inr(cgst_paise)} does not equal SGST {_inr(sgst_paise)}; "
                "intra-state GST is split equally"
            ),
        )
    return Validation(
        id="tax_split",
        ok=True,
        detail=f"CGST {_inr(cgst_paise)} equals SGST {_inr(sgst_paise)}",
    )


def validate_invoice(
    *,
    supplier_gstin: str,
    taxable_paise: int,
    cgst_paise: int | None,
    sgst_paise: int | None,
    igst_paise: int | None,
    total_paise: int,
) -> list[Validation]:
    """Every deterministic validation the India GST path runs on one invoice."""
    return [
        validate_gstin(supplier_gstin),
        *validate_tax(
            taxable_paise=taxable_paise,
            cgst_paise=cgst_paise,
            sgst_paise=sgst_paise,
            igst_paise=igst_paise,
            total_paise=total_paise,
        ),
    ]


def _inr(paise: int) -> str:
    return f"{paise / 100:,.2f} INR"
