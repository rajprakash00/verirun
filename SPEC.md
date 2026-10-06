# Verirun — Product Spec

## Problem Statement

Verirun is a working prototype: one unchanged engine turns a short Request into completed work against a self-contained Mock Suite, with a Work Order, Approval Gates, an independent Verifier, and an Evidence Pack.

It is not yet a product:

- **The live path cannot do what the Task Packs promise.** Browser Tools are registered only in the demo script and tests, so a live `run` or a dashboard resume can plan `browser.*` Steps that no tool can serve.
- **Public claims drift from the code.** Policies name actions with no tools (`mail.send`, `files.delete`, `payment.split`); the architecture promises a fallback LLM provider, discrete `Approve` / `Observe` / `Report` states, and a demo recording that do not exist.
- **The name collides.** "Operator" matches a sunset product and many agent startups.
- **The core claim is unmeasured.** "Independent verification catches what the agent would miss" is asserted but never quantified. There is no eval harness, no ablation, and no aggregate quality signal.
- **Browser control is brittle.** Steps depend on one-shot accessibility-tree references; a small UI change fails the Step and burns the Run.
- **There is no license, CI, or contributor surface.** The public repository is legally unusable and cannot prove it stays green.

## Solution

Turn the prototype into a polished, reproducible Verirun:

1. Rename the project to Verirun and license it MIT.
2. Make every public claim true.
3. Make the live path complete, including browser Tools.
4. Make browser control self-healing: recover a lost control by role and name, keep retries idempotent.
5. Measure the verification claim: an embedded eval harness with Verifier-on/off and Approval-Gate-on/off ablations, published with honest failure cases.
6. Package it: one-command demo, a short recording, a hosted sample Evidence Pack, and a standard repository/community baseline.

The Mock Suite stays the demo ground truth. The engine, the Task Packs, and the trust model do not change.

## User Stories

1. As a new user, I want the project named Verirun everywhere (package, CLI, environment variables, docs, glossary), so that I do not confuse it with other tools.
2. As a new user, I want an MIT LICENSE, so that I know I may use and modify the code.
3. As a new user, I want accurate package metadata (name, license, readme, URLs, classifiers), so that installers and indexes describe the project correctly.
4. As a reader, I want the glossary, README, SOPs, policies, and architecture to use Verirun as the system name, so that the vocabulary stays consistent.
5. As a reviewer, I want every documented Tool, state, and feature to exist in the code, so that I can trust the documentation.
6. As a compliance officer, I want policies to reference only actions Verirun can actually attempt, so that policy evaluation is meaningful.
7. As a reviewer, I want the architecture document to describe the state machine as it is implemented, so that the design document is accurate.
8. As an operator, I want a live `run` to execute `browser.*` Steps against the Mock Suite, so that the live path matches the Task Pack allowlists.
9. As an operator, I want the dashboard to register the same Tools as the CLI, so that an approval-resumed Run behaves like a fresh Run.
10. As an operator, I want a browser Step that loses its target reference to recover automatically, so that a small UI change does not fail the Run.
11. As an operator, I want the recovered locator recorded in the Run journal, so that a retry touches the same control and does not repeat a completed side effect.
12. As an operator, I want a vision fallback when the accessibility tree does not contain the control, so that image-only widgets are still reachable.
13. As an operator, I want re-grounding bounded by the existing failure ladder (retry, alternate strategy, re-plan, escalate), so that recovery cannot loop.
14. As an engineer, I want re-grounding covered by tests that run a real browser against a mock page that changes, so that the behavior does not regress.
15. As a reviewer, I want a seeded "UI changed" scenario that shows recovery end to end, so that the capability is demonstrable.
16. As a reviewer, I want an eval harness that runs the seeded scenarios in replay mode and prints one report, so that I can judge a change without a live model.
17. As a reviewer, I want per-Run metrics (task success, Verification pass, human interventions, steps, cost, resume correctness), so that quality is visible.
18. As a reviewer, I want an ablation that disables the Verifier, so that I can see how many failures go undetected without independent verification.
19. As a reviewer, I want an ablation that disables Approval Gates, so that I can see how many irreversible actions would be submitted without them.
20. As a reviewer, I want the ablation results published in the README with honest failure cases, so that the core claim is evidence, not assertion.
21. As a contributor, I want CI that runs the test suite and lint on every push and pull request, so that the main branch stays green.
22. As a contributor, I want a coverage signal, so that gaps are visible.
23. As a new user, I want one command that seeds the Mock Suite and runs the demo, so that I can see a Run in minutes.
24. As a new user, I want a short recording of a Run recovering from a failure, pausing at an Approval Gate, and passing Verification, so that I understand the product without installing it.
25. As a reviewer, I want a hosted sample Evidence Pack that opens standalone in a browser, so that I can inspect the output without running anything.
26. As a contributor, I want CONTRIBUTING, SECURITY, issue templates, a CHANGELOG, and a `v0.1.0` tag, so that I know how to participate and what changed.
27. As an engineer, I want the replay fixtures extended to the scenarios the eval harness runs, so that the evaluation is deterministic and offline.
28. As an engineer, I want the README's first screen to present the problem, the quickstart, the architecture, and the evaluation results, so that the project explains itself in four minutes.
29. As an engineer, I want the Known Limitations section to list exactly what is not built, so that expectations stay honest.

## Implementation Decisions

**Rename (wide mechanical refactor).** The Python package, CLI entry point, environment-variable prefix, fixture keys, docs, and glossary move from Operator naming to Verirun. No external consumers exist, so this lands as one mechanical change with a green test suite. The GitHub repository rename is a human step performed after the change lands; links and the remote URL update follow it.

**Live path.** The runtime that builds the Tool registry gains the browser Tools whenever the CLI and dashboard start a Run. The dashboard and CLI share one construction path, so they cannot drift again.

**Re-grounding.** When a browser action fails because a reference is stale or missing, the browser Tool takes a fresh accessibility-tree snapshot, re-resolves the target by role and accessible name, and retries with the new reference. If the tree does not contain the control, the existing vision path inspects the screen. The successful locator is stored in the Run journal; a retry replays the same locator, preserving idempotency. Attempts are bounded by the existing failure ladder.

**Eval harness.** A developer-facing command runs the seeded scenarios in replay mode against the real Mock Suite and produces one report. Metrics per Run: terminal state, Verification pass/fail per criterion, human interventions, steps, cost, and whether a resumed Run finished correctly. The harness asserts on report shape and known scenario outcomes, not transcript text.

**Ablations.** Eval-only configuration switches disable the Verifier and the Approval Gates. The report contrasts detection rates: failures the Verifier catches, failures that pass unnoticed with the Verifier off, and irreversible actions prepared without a gate. Results appear in the README with failure cases, not just aggregate numbers.

**Docs truth.** Every phantom action is either implemented or removed from policy references; the architecture document is corrected to the implemented state machine; the fallback-provider claim is removed or the feature is marked not built. The glossary rename keeps one canonical name per concept.

**Packaging.** A single demo command seeds and runs the Mock Suite. The recording covers recovery, an Approval Gate, and Verification. One sample Evidence Pack is published as a static page. Repository baseline adds LICENSE, CI, coverage, community files, and a `v0.1.0` tag.

**Repository content boundary.** The repository and its issues describe the product and its engineering only. They do not carry personal, career, marketing, or launch-planning content.

## Testing Decisions

A good test asserts external behavior only: run state, Mock Suite ground truth, the Evidence Pack, dashboard responses, and the eval report. No tests on private functions or transcript text.

- **Highest seam (preferred):** drive the whole engine in-process against the real Mock Suite with a replay or scripted LLM. Prior art: the walking-skeleton, acceptance, and demo-script tests.
- **Browser seam:** real Chromium against the mock applications, including a page variant that invalidates old references. Prior art: the browser Tool tests.
- **HTTP seam:** the dashboard API, asserting browser Tools are registered in the live path and that approvals resume correctly. Prior art: the dashboard tests.
- **Eval seam:** the harness report, asserting metric shape and the known terminal states of the seeded scenarios.
- **Unit seams:** only the existing ones (Task Pack validation, Policy evaluation, Run store) where table-driven tests already exist.

## Out of Scope

- Real third-party systems, credentials, payments, or email sending; all effects stay simulated in the Mock Suite.
- Authentication, multi-tenancy, and cloud deployment.
- New Task Packs or general-purpose task coverage beyond the two demonstrated.
- Long-term learning or memory beyond committed precedent documents.
- A standalone public benchmark, leaderboard, or external adoption surface.
- Queueing, scheduling, or production performance work.
- Personal, career, marketing, or launch-planning material in the repository.

## Further Notes

- Name availability was checked at PyPI, GitHub, and DNS level (`verirun` package and repository name free; `verirun.dev` and `verirun.io` free). Trademark clearance has not been performed.
- The Mock Suite remains test infrastructure and demo ground truth, not the product.
- If locator journaling proves hard to reverse, record it as an ADR when it lands.
- The delivered prototype-phase spec is archived at [`docs/specs/0001-prototype.md`](docs/specs/0001-prototype.md).
- Implementation tickets live in the GitHub tracker under the `ready-for-agent` label.
