from pathlib import Path

import pytest

from company_operator.context.company import (
    CompanyContextError,
    load_company_context,
)

POLICY = """\
id: spend-limits
title: Spend limits
rules:
  - id: approval-threshold
    description: Above the limit a human decides.
    effect: require_approval
    actions: [payment.schedule]
    when:
      - field: amount_usd
        op: gt
        value: 10000
"""

SYSTEMS = """\
systems:
  - id: maildesk
    title: MailDesk
    kind: web
    base_url: http://127.0.0.1:8001
    description: The mailbox.
    credentials:
      username: ap@company.example
      password: maildesk
  - id: files
    title: Shared files
    kind: filesystem
    root: shared
"""


def build_context_tree(root: Path) -> Path:
    (root / "sops").mkdir(parents=True)
    (root / "policies").mkdir()
    (root / "precedents").mkdir()
    (root / "sops" / "invoice-processing.md").write_text(
        "# Invoice processing\n\nMatch invoices to purchase orders.\n", encoding="utf-8"
    )
    (root / "policies" / "spend-limits.yaml").write_text(POLICY, encoding="utf-8")
    (root / "systems.yaml").write_text(SYSTEMS, encoding="utf-8")
    (root / "precedents" / "duplicate-invoice.md").write_text(
        "# Duplicate invoice\n\nNever file the same invoice twice.\n", encoding="utf-8"
    )
    return root


def test_loads_all_context_parts(tmp_path: Path) -> None:
    context = load_company_context(build_context_tree(tmp_path))

    sop = context.sops["invoice-processing"]
    assert sop.title == "Invoice processing"
    assert "Match invoices to purchase orders." in sop.body

    policy = context.policies["spend-limits"]
    assert policy.title == "Spend limits"
    assert policy.rules[0].id == "approval-threshold"

    assert context.systems["maildesk"].base_url == "http://127.0.0.1:8001"
    assert context.systems["maildesk"].credentials["password"] == "maildesk"
    assert context.systems["files"].kind == "filesystem"

    assert context.precedents[0].id == "duplicate-invoice"
    assert "Never file the same invoice twice." in context.precedents[0].body


def test_lookup_helpers_raise_clear_errors(tmp_path: Path) -> None:
    context = load_company_context(build_context_tree(tmp_path))

    assert context.sop("invoice-processing").id == "invoice-processing"
    assert context.policy("spend-limits").id == "spend-limits"
    assert context.system("files").id == "files"

    with pytest.raises(CompanyContextError, match="no SOP 'nope'"):
        context.sop("nope")
    with pytest.raises(CompanyContextError, match="no policy 'nope'"):
        context.policy("nope")


def test_missing_root_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(CompanyContextError, match="company context not found"):
        load_company_context(tmp_path / "missing")


def test_bad_policy_file_names_the_file(tmp_path: Path) -> None:
    root = build_context_tree(tmp_path)
    (root / "policies" / "broken.yaml").write_text("id: [oops\n", encoding="utf-8")

    with pytest.raises(CompanyContextError) as excinfo:
        load_company_context(root)

    assert "broken.yaml" in str(excinfo.value)


def test_duplicate_policy_id_is_rejected(tmp_path: Path) -> None:
    root = build_context_tree(tmp_path)
    (root / "policies" / "spend-limits-copy.yaml").write_text(POLICY, encoding="utf-8")

    with pytest.raises(CompanyContextError, match="duplicate policy id 'spend-limits'"):
        load_company_context(root)


def test_duplicate_system_id_is_rejected(tmp_path: Path) -> None:
    root = build_context_tree(tmp_path)
    (root / "systems.yaml").write_text(
        SYSTEMS + "\n  - id: maildesk\n    title: Again\n    kind: web\n", encoding="utf-8"
    )

    with pytest.raises(CompanyContextError, match="duplicate system id 'maildesk'"):
        load_company_context(root)


def test_shipped_company_context_matches_the_task_pack() -> None:
    root = Path(__file__).resolve().parents[1]
    context = load_company_context(root / "company")

    assert "invoice-processing" in context.sops
    assert "vendor-onboarding" in context.sops
    assert {"spend-limits", "action-rules"} <= set(context.policies.ids())
    assert {"maildesk", "ledgerlite", "files"} <= set(context.systems)
    assert context.precedents

    decision = context.policies.evaluate(
        "payment.schedule", {"amount_usd": 12500, "currency": "USD"}
    )
    assert decision.outcome == "require_approval"
    assert decision.rule == "approval-threshold"
