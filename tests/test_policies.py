import pytest

from verirun.context.policies import (
    Policy,
    PolicyEvaluationError,
    PolicyRule,
    PolicySet,
    Predicate,
)


def rule(
    rule_id: str,
    effect: str,
    actions: list[str] | None = None,
    when: list[Predicate] | None = None,
) -> PolicyRule:
    return PolicyRule(
        id=rule_id,
        description=f"{rule_id} description",
        effect=effect,
        actions=actions or [],
        when=when or [],
    )


SPEND_LIMITS = Policy(
    id="spend-limits",
    title="Spend limits",
    rules=[
        rule(
            "approval-threshold",
            "require_approval",
            actions=["payment.schedule"],
            when=[Predicate(field="amount_usd", op="gt", value=10000)],
        ),
        rule("no-split-payments", "forbid", actions=["payment.split"]),
    ],
)

ACTION_RULES = Policy(
    id="action-rules",
    title="Action rules",
    rules=[
        rule(
            "no-payment-to-blocked-vendor",
            "forbid",
            actions=["payment.schedule"],
            when=[Predicate(field="vendor_status", op="eq", value="blocked")],
        ),
        rule("no-external-email", "require_approval", actions=["mail.send"]),
        rule("no-files-delete", "forbid", actions=["files.delete"]),
        rule("no-other-system", "forbid", when=[Predicate(field="system", op="ne", value="ledgerlite")]),
    ],
)

POLICIES = PolicySet([SPEND_LIMITS, ACTION_RULES])


@pytest.mark.parametrize(
    ("action", "facts", "outcome", "policy_id", "rule_id"),
    [
        pytest.param("files.read", {}, "allow", None, None, id="unmatched-action"),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 500, "vendor_status": "active", "system": "ledgerlite"},
            "allow",
            None,
            None,
            id="within-limit",
        ),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 10000, "vendor_status": "active", "system": "ledgerlite"},
            "allow",
            None,
            None,
            id="exactly-at-limit-is-allowed",
        ),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 10000.01, "vendor_status": "active", "system": "ledgerlite"},
            "require_approval",
            "spend-limits",
            "approval-threshold",
            id="above-limit-needs-approval",
        ),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 50, "vendor_status": "blocked", "system": "ledgerlite"},
            "forbid",
            "action-rules",
            "no-payment-to-blocked-vendor",
            id="forbid-beats-approval",
        ),
        pytest.param(
            "payment.split",
            {"amount_usd": 999999, "vendor_status": "active", "system": "ledgerlite"},
            "forbid",
            "spend-limits",
            "no-split-payments",
            id="split-payments-forbidden",
        ),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 10, "vendor_status": "active", "system": "ledgerlite"},
            "allow",
            None,
            None,
            id="payment-in-ledgerlite-allowed",
        ),
        pytest.param(
            "payment.schedule",
            {"amount_usd": 10, "vendor_status": "active", "system": "other"},
            "forbid",
            "action-rules",
            "no-other-system",
            id="payment-outside-ledgerlite-forbidden",
        ),
        pytest.param("mail.send", {}, "require_approval", "action-rules", "no-external-email", id="mail-needs-approval"),
        pytest.param("files.delete", {}, "forbid", "action-rules", "no-files-delete", id="delete-forbidden"),
    ],
)
def test_policy_evaluation_table(
    action: str,
    facts: dict,
    outcome: str,
    policy_id: str | None,
    rule_id: str | None,
) -> None:
    decision = POLICIES.evaluate(action, facts)

    assert decision.outcome == outcome
    assert decision.policy == policy_id
    assert decision.rule == rule_id
    assert decision.reason


def test_missing_fact_does_not_match_a_condition() -> None:
    decision = POLICIES.evaluate("payment.schedule", {"amount_usd": 50})

    assert decision.outcome == "allow"


def test_incomparable_fact_raises_instead_of_failing_open() -> None:
    with pytest.raises(PolicyEvaluationError, match="cannot evaluate 'amount_usd'"):
        POLICIES.evaluate("payment.schedule", {"amount_usd": "not-a-number"})


def test_forbid_wins_over_require_approval_across_policies() -> None:
    decisions = [
        POLICIES.evaluate(
            "payment.schedule",
            {"amount_usd": 999999, "vendor_status": "blocked", "system": "ledgerlite"},
        )
    ]

    assert decisions[0].outcome == "forbid"
    assert decisions[0].policy == "action-rules"


def test_policies_by_id_and_missing_lookup() -> None:
    assert POLICIES["spend-limits"] is SPEND_LIMITS
    with pytest.raises(KeyError):
        POLICIES["nope"]


def test_invalid_effect_is_rejected() -> None:
    with pytest.raises(ValueError):
        PolicyRule(id="x", description="x", effect="maybe")


def test_all_condition_operators() -> None:
    facts = {"n": 5, "name": "alpha", "tags": ["a", "b"]}
    cases = [
        (Predicate(field="n", op="eq", value=5), True),
        (Predicate(field="n", op="ne", value=6), True),
        (Predicate(field="n", op="gt", value=4), True),
        (Predicate(field="n", op="gte", value=5), True),
        (Predicate(field="n", op="lt", value=6), True),
        (Predicate(field="n", op="lte", value=5), True),
        (Predicate(field="name", op="in", value=["alpha", "beta"]), True),
        (Predicate(field="tags", op="contains", value="a"), True),
        (Predicate(field="n", op="gt", value=5), False),
        (Predicate(field="missing", op="eq", value=1), False),
    ]
    for predicate, expected in cases:
        assert predicate.matches(facts) is expected, predicate
