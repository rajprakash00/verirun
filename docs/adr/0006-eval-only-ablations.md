# ADR 0006: Eval-only ablations for the Verifier and the Approval Gates

Status: accepted
Date: 2026-10-19
Task: #32 — Ablations and published results

## Context

The product spec claims that independent verification catches failures the doer
would miss, and that Approval Gates stop irreversible actions until a human says
yes. Neither claim was measured. The eval harness (#31) runs the seeded
scenarios in replay mode and reports per-Run metrics, but it always runs the
product path: the Verifier checks every outcome and the policy gate parks every
gated action.

To measure the controls, the harness needs to run the same scenarios with one
control disabled at a time, without turning either control into a product
setting. The comparison needs failures the doer reports as success (otherwise
the Verifier has nothing to catch) and irreversible actions that normally wait
at a gate.

## Decision

Add two switches to the eval harness only: `verifier` and `approval_gates`, both
defaulting to `True`. The product paths (`run`, `serve`, `report`, the dashboard)
never pass them, so their behavior is unchanged.

1. **Verifier off.** Execution runs normally; when the Run reaches `verifying`
   the engine accepts the executor's outcome and moves to `completed` without a
   verification pass. The unverified Run writes no Evidence Pack, so no artifact
   claims an unchecked outcome was verified.
2. **Approval Gates off.** The policy gate converts a `require_approval`
   decision into `allow`; a `forbid` decision is untouched. Policy denials are
   not Approval Gates, so a blocked vendor payment still escalates.
3. **Probes.** Two silent-failure scenarios deliberately overclaim: one calls no
   tools, one files the invoice against the wrong vendor. The Verifier catches
   both against ground truth; with the Verifier off both end `completed`.
4. **Report.** A baseline report is written alongside the two ablation passes.
   The report contrasts caught failures and failed checks, the failure rate the
   Verifier detects and the rate that goes undetected without it, gated versus
   ungated irreversible actions, and the policy denials that still held. The
   report fails when a probe is not caught, a probe is not accepted without the
   Verifier, a gated action does not execute without gates, or a policy denial
   stops holding.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **A product setting (`VERIRUN_VERIFIER=off`)** | The spec calls for eval-only switches. A product setting would let a deployment turn off its own trust controls. |
| **Run the Verifier but ignore its verdict** | It still pays the verification cost and still writes verification records, so the contrast measures the report rather than the control. |
| **Disable the whole policy gate** | It would also stop enforcing `forbid` rules, which would make the ablation look better than the control it is meant to isolate. |
| **Measure live model traffic** | Non-deterministic, paid, and not reproducible offline. The committed probes demonstrate the mechanism; they are not a benchmark sample. |

## Consequences

- The claim is now evidence with a reproducible command, not an assertion.
- Verification still only catches what the Task Pack contract checks; a defect
  outside the contract passes in both modes.
- The probes are constructed to overclaim, so the 2/2 detection rate proves the
  mechanism, not live-model quality.
- The engine gains two default-on parameters used only by the harness. Future
  ablations should follow the same pattern rather than adding product settings.
