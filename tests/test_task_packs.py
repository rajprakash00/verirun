import textwrap
from pathlib import Path

import pytest

from company_operator.context.task_pack import TaskPackError, load_task_pack

VALID = """\
id: sample-task
title: Sample task
goal_template: "Do the thing: {request}"
sop: sample-sop
policies:
  - spend-limits
tools:
  - files.read
  - erp.file_invoice
approval_rules:
  - id: over-limit
    description: Above the limit a human decides.
    policy: spend-limits
    actions:
      - payment.schedule
verification:
  - id: filed
    description: It is filed.
    check: erp_invoice_matches
    params:
      match: [invoice_number]
"""


def write_pack(tmp_path: Path, text: str, name: str = "sample-task.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_valid_pack_loads(tmp_path: Path) -> None:
    pack = load_task_pack(write_pack(tmp_path, VALID))

    assert pack.id == "sample-task"
    assert pack.title == "Sample task"
    assert pack.sop == "sample-sop"
    assert pack.policies == ["spend-limits"]
    assert pack.tools == ["files.read", "erp.file_invoice"]
    assert pack.approval_rules[0].id == "over-limit"
    assert pack.approval_rules[0].policy == "spend-limits"
    assert pack.verification[0].check == "erp_invoice_matches"
    assert pack.verification[0].params == {"match": ["invoice_number"]}


def test_extraction_threshold_is_optional_and_parses(tmp_path: Path) -> None:
    pack = load_task_pack(write_pack(tmp_path, VALID))
    assert pack.extraction is None

    with_threshold = VALID + "\nextraction:\n  confidence_threshold: 0.8\n"
    pack = load_task_pack(write_pack(tmp_path, with_threshold))

    assert pack.extraction is not None
    assert pack.extraction.confidence_threshold == 0.8


@pytest.mark.parametrize("threshold", ["1.5", "-0.1", "not-a-number"])
def test_extraction_threshold_must_be_a_probability(tmp_path: Path, threshold: str) -> None:
    text = VALID + f"\nextraction:\n  confidence_threshold: {threshold}\n"

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "confidence_threshold" in str(excinfo.value)


def test_files_extract_requires_an_extraction_threshold(tmp_path: Path) -> None:
    text = VALID.replace(
        "tools:\n  - files.read\n  - erp.file_invoice",
        "tools:\n  - files.read\n  - files.extract\n  - erp.file_invoice",
    )

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "files.extract" in str(excinfo.value)
    assert "confidence_threshold" in str(excinfo.value)


def test_minimal_pack_loads_with_empty_optionals(tmp_path: Path) -> None:
    pack = load_task_pack(
        write_pack(
            tmp_path,
            """
            id: minimal
            goal_template: "Do it: {request}"
            sop: some-sop
            tools: [files.read]
            """,
        )
    )

    assert pack.policies == []
    assert pack.approval_rules == []
    assert pack.verification == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param(
            {"id": ""},
            "id: String should match pattern",
            id="empty-id",
        ),
        pytest.param(
            {"id": "Bad Id"},
            "id: String should match pattern",
            id="id-with-spaces",
        ),
        pytest.param(
            {"goal_template": ""},
            "goal_template: String should have at least 1 character",
            id="empty-goal-template",
        ),
        pytest.param(
            {"sop": ""},
            "sop: String should have at least 1 character",
            id="empty-sop",
        ),
        pytest.param(
            {"tools": []},
            "tools: List should have at least 1 item",
            id="no-tools",
        ),
    ],
)
def test_invalid_field_is_rejected_with_a_precise_error(
    tmp_path: Path, mutation: dict, message: str
) -> None:
    pack = {
        "id": "sample-task",
        "goal_template": "Do the thing: {request}",
        "sop": "sample-sop",
        "tools": ["files.read"],
    }
    pack.update(mutation)
    text = "\n".join(f"{key}: {value!r}" for key, value in pack.items())

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert str(excinfo.value).startswith("invalid Task Pack")
    assert "sample-task.yaml" in str(excinfo.value)
    assert message in str(excinfo.value)


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "id: sample-task\ngoal_template: do it\n")

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(path)

    assert "sop: Field required" in str(excinfo.value)
    assert "tools: Field required" in str(excinfo.value)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    text = VALID + "\nunknown_field: nope\n"

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "unknown_field: Extra inputs are not permitted" in str(excinfo.value)


def test_duplicate_tool_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace("tools:\n  - files.read\n  - erp.file_invoice", "tools: [files.read, files.read]")

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "duplicate tool" in str(excinfo.value)
    assert "files.read" in str(excinfo.value)


def test_duplicate_policy_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace("policies:\n  - spend-limits", "policies: [spend-limits, spend-limits]")

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "duplicate policy" in str(excinfo.value)


def test_duplicate_approval_rule_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace(
        "approval_rules:\n  - id: over-limit",
        "approval_rules:\n  - id: over-limit\n    description: one",
    )
    text = text.replace(
        "    actions:\n      - payment.schedule",
        "    actions:\n      - payment.schedule\n  - id: over-limit\n    description: two",
    )

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(write_pack(tmp_path, text))

    assert "duplicate approval rule" in str(excinfo.value)


def test_bad_yaml_is_rejected_with_a_precise_error(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "id: [unclosed\n")

    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(path)

    assert "invalid YAML" in str(excinfo.value)
    assert "sample-task.yaml" in str(excinfo.value)


def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(TaskPackError) as excinfo:
        load_task_pack(tmp_path / "nope.yaml")

    assert "not found" in str(excinfo.value)
    assert "nope.yaml" in str(excinfo.value)


def test_shipped_invoice_pack_is_valid() -> None:
    from company_operator.context.company import load_company_context

    root = Path(__file__).resolve().parents[1]
    pack = load_task_pack(root / "tasks" / "invoice-processing.yaml")
    context = load_company_context(root / "company")

    assert pack.id == "invoice-processing"
    assert pack.sop in context.sops
    for policy in pack.policies:
        assert policy in context.policies
    assert "files.extract" in pack.tools
    assert pack.extraction is not None
    assert 0.0 <= pack.extraction.confidence_threshold <= 1.0


def test_shipped_vendor_onboarding_pack_is_valid() -> None:
    from company_operator.context.company import load_company_context

    root = Path(__file__).resolve().parents[1]
    pack = load_task_pack(root / "tasks" / "vendor-onboarding.yaml")
    context = load_company_context(root / "company")

    assert pack.id == "vendor-onboarding"
    assert pack.sop in context.sops
    for policy in pack.policies:
        assert policy in context.policies
    assert "erp.create_vendor" in pack.tools
    assert pack.extraction is None
    check_ids = [check.id for check in pack.verification]
    assert check_ids == ["vendor-created", "tax-document-archived"]
    assert pack.approval_rules[0].actions == ["vendor.create"]
