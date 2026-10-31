"""Tests for India GST validation: the GSTIN check digit and the tax arithmetic.

The expected outcomes come from published, known-valid GSTINs and from the
invoice arithmetic itself. The test supplier's GSTIN was generated once from the
published algorithm and is pinned here as a literal, so the tests never call the
code under test to derive an expectation.
"""

from __future__ import annotations

from verirun.india import validate_gstin, validate_tax

# Two GSTINs published as valid examples, and one generated from the same
# algorithm with a valid check digit for the test supplier.
VALID_GSTINS = (
    "27AAPFU0939F1ZV",
    "29AAGCB7383J1Z4",
    "29AABCV1234C1ZQ",
)


def test_known_valid_gstins_pass_the_checksum() -> None:
    for gstin in VALID_GSTINS:
        result = validate_gstin(gstin)

        assert result.ok, f"{gstin}: {result.detail}"
        assert result.id == "gstin_checksum"


def test_gstin_is_normalised_before_validation() -> None:
    result = validate_gstin(" 29aabcv 1234c1zq ")

    assert result.ok
    assert "29AABCV1234C1ZQ" in result.detail


def test_a_wrong_check_digit_is_caught_and_explained() -> None:
    result = validate_gstin("29AABCV1234C1ZP")

    assert not result.ok
    assert "check digit" in result.detail
    assert "'Q'" in result.detail and "'P'" in result.detail


def test_malformed_gstins_are_caught() -> None:
    for gstin in (
        "29AABCV1234C1",
        "29AABCV1234C1ZQ9",
        "00AABCV1234C1ZQ",
        "29AABCV1234C1XQ",
        "29AABCV1234C1Z?",
        "",
    ):
        result = validate_gstin(gstin)

        assert not result.ok, gstin
        assert "15-character GSTIN" in result.detail


def test_tax_components_sum_to_the_total() -> None:
    results = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=90_000,
        igst_paise=None,
        total_paise=1_180_000,
    )

    assert [result.id for result in results] == ["tax_sum", "tax_split"]
    assert all(result.ok for result in results), [result.detail for result in results]
    assert "11,800.00 INR" in results[0].detail


def test_a_tax_sum_that_does_not_add_up_is_caught() -> None:
    results = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=90_000,
        igst_paise=None,
        total_paise=1_185_000,
    )

    tax_sum = next(result for result in results if result.id == "tax_sum")
    assert not tax_sum.ok
    assert "11,800.00 INR" in tax_sum.detail
    assert "11,850.00 INR" in tax_sum.detail


def test_an_igst_invoice_cannot_also_carry_cgst_and_sgst() -> None:
    results = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=90_000,
        igst_paise=180_000,
        total_paise=1_360_000,
    )

    split = next(result for result in results if result.id == "tax_split")
    assert not split.ok
    assert "IGST" in split.detail


def test_cgst_and_sgst_must_be_present_together_and_equal() -> None:
    one_sided = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=None,
        igst_paise=None,
        total_paise=1_090_000,
    )
    unequal = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=45_000,
        igst_paise=None,
        total_paise=1_135_000,
    )

    assert not next(r for r in one_sided if r.id == "tax_split").ok
    assert not next(r for r in unequal if r.id == "tax_split").ok


def test_an_invoice_without_tax_components_is_allowed() -> None:
    results = validate_tax(
        taxable_paise=1_000_000,
        cgst_paise=None,
        sgst_paise=None,
        igst_paise=None,
        total_paise=1_000_000,
    )

    assert all(result.ok for result in results)
