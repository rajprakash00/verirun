# ADR 0005: Browser re-grounding: re-resolve by role and name, fall back to vision

Status: accepted
Date: 2026-10-08
Task: #30 — Browser re-grounding

## Context

Browser control addresses elements through refs minted by the last
accessibility snapshot (ADR 0003). Refs are page-scoped: any re-render or
navigation invalidates them, and until now a stale ref failed the Step with a
`not_found` Observation, burning the Run on a small UI change. The product spec
asks for a self-healing browser Step: fresh snapshot, re-resolve by role and
accessible name, retry with the new ref, a vision fallback when the tree lacks
the control, and a journaled locator so retries stay idempotent and do not
repeat a completed side effect.

## Decision

Make re-grounding a property of the browser session, invisible to the model.

1. A session remembers the role and accessible name behind every ref handed to
   the model, and the last ref a re-grounding resolved for each requested ref.
2. When a ref no longer resolves, the session takes a fresh snapshot, matches
   the target by role and accessible name (exact, then case-insensitive, then a
   unique substring), and retries with the new ref. A previously resolved
   locator is replayed first when it is still present and still matches.
3. When the tree does not contain the control, the session screenshots the
   viewport and asks the vision model for the control's center under a strict
   JSON schema. The action runs on the element at that point, and the point is
   refused when the element there contradicts the wanted role or name.
4. The resolution an action used -- role, name, method, ref or point -- is
   recorded in its Observation. Side-effect tools are journaled by the engine,
   so a retry or a resume replays the same locator instead of repeating a
   completed side effect. A vision point is replayed for the same ref while the
   page URL is unchanged.
5. Failures stay inside the fixed escalation ladder: a target that cannot be
   found in the tree or on screen surfaces as one `not_found` Observation, and
   retry, alternate strategy, re-plan, and escalate stay the engine's call.

The `?ui=changed` and `?ui=painted` variants of LedgerLite's new-invoice page
seed the "UI changed" scenario: the page re-renders itself right after the
first snapshot, so refs go stale deterministically, and the painted variant
turns the submit button into a non-semantic div that only vision can reach.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Fail and let the model re-snapshot** | Small UI changes burn Steps and tokens, and the model must first notice the ref died. The product spec asks for recovery inside the Step. |
| **CSS selectors or stable ids** | App-specific and unbounded; the model would guess implementation details instead of reading semantics (ADR 0003). |
| **Vision-first for every action** | Costs image tokens per Step, coordinates are brittle, and the audit trail becomes pixels instead of named controls. Kept strictly as a fallback for what the tree lacks. |
| **Cache a locator in the Run store, not the Observation** | The Observation is already the journal's payload; a second store would duplicate the ledger and split idempotency in two. |

## Consequences

- A UI re-render no longer fails the Step when the control still has a role and
  an accessible name, and an image-only widget is still reachable.
- Locator decisions are auditable: every Observation names the role, name, and
  method it used, and the Evidence Pack's action log carries it.
- Every recovery attempt can cost a model call; the vision fallback's usage and
  cost are reported in the Observation so the Run cost meter counts them.
- Internal re-grounding snapshots renumber DOM refs behind the model's back;
  the identity check in `resolve` detects a ref that now names a different
  element, and the tool re-grounds rather than acting on the wrong control.
