# ADR 0001: Generic vendor write tool and vendor verification check

Status: accepted
Date: 2026-10-03
Task: #12 — Vendor onboarding Task Pack (generalization)

## Context

The second Task Pack, `vendor-onboarding`, must run on the unchanged engine and
tool layer. Everything the invoice flow needs already exists: the ReAct loop,
the policy gate and approval gates, escalation, archiving, and file
verification. Two genuinely missing generic hooks remained:

- the ERP tools could read vendors but not create one, and
- the Verifier had contract checks for invoices and archived files but none for
  a created vendor record.

Both are generic capabilities, not vendor-specific business logic: any Task Pack
that writes a vendor would need them.

## Decision

Add two additive, task-agnostic hooks.

1. `erp.create_vendor` in `src/company_operator/tools/erp.py`. A generic
   LedgerLite write tool: `side_effect = True`, `irreversible = True`, action
   `vendor.create`. It records the legal name, tax id, address, and contact
   email, rejects a vendor whose tax id or name already exists, reporting the
   existing record, and never invents a tax id. `vendor.create` is
   approval-gated by the new `vendor-management` policy, so the existing engine
   parks the Run before submission; no engine change was needed.
2. `erp_vendor_matches` in `src/company_operator/engine/verify.py`. A generic
   check over a Run's completed `erp.create_vendor` claims: the vendor exists in
   LedgerLite exactly once for its tax id and name, and the legal name, tax id,
   address, and contact email agree with the source tax form read independently
   by the Verifier. Copies of the form are tolerated when they agree.

The state machine, tool interface, policy gate, escalation ladder, and Run
store are untouched. The duplicate and missing-tax-form paths use the existing
engine-owned `task.escalate` tool.

## Consequences

- Task Packs can now create vendors and verify them without engine changes, the
  same way invoice-processing files invoices.
- The verifier check registry stays pluggable; the new check reads ground truth
  (LedgerLite and the PDF) and never the executor's messages.
- `vendor.create` is a new policy action; future write tools should follow the
  same pattern: declare the action, gate it in a policy, and add a ground-truth
  check.
- The Mock Suite grows per-scenario onboarding seeds (happy, duplicate, missing
  tax form) so the behavior is demonstrable and testable end to end.
