# Verirun

[![CI](https://github.com/rajprakash00/verirun/actions/workflows/ci.yml/badge.svg)](https://github.com/rajprakash00/verirun/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/rajprakash00/verirun/branch/main/graph/badge.svg)](https://codecov.io/gh/rajprakash00/verirun)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The problem.** Company work arrives as a short request — "process the
invoices" — but completing it means reading mail, checking an ERP, filing
documents, driving a browser, and knowing when a human must decide. An agent
that only reports what it did cannot be trusted with that work. Verirun is a
compact agent runtime that plans the work, executes it with real tools, recovers
from failures, stops at **Approval Gates** before irreversible actions, and
**verifies the outcome against ground truth** before calling it done.

One unchanged engine runs two Task Packs — invoice processing and vendor
onboarding — against a self-contained Mock Suite. The demo is offline,
deterministic, and needs no API key.

- Product spec: [`SPEC.md`](SPEC.md)
- Design document: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Glossary: [`CONTEXT.md`](CONTEXT.md)
- [Watch the short recording](docs/media/demo.webm) ·
  [Open a sample Evidence Pack](https://rajprakash00.github.io/verirun/sample-evidence/)

## Quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and headless Chromium
(Playwright downloads its own copy).

```bash
git clone https://github.com/rajprakash00/verirun.git
cd verirun
uv sync
uv run playwright install chromium          # add --with-deps on a fresh Linux box
uv run verirun demo
```

`verirun demo` resets and seeds the Mock Suite, then runs three scenarios on the
unchanged engine: a happy path, a transient ERP failure that the engine retries,
and an over-limit payment that parks at an Approval Gate until a human approves
it. It prints each Run's state, recovery, verification result, and Evidence Pack
path; a completed Run is visible in under a minute. Everything runs offline and
free. Pass `--no-browser` to skip the screenshot, or `--state-dir`, `--shared`,
and `--runs` to relocate state.

Prefer not to install it? [Watch the recording](#the-demo) and
[open the sample Evidence Pack](#sample-evidence-pack).

## Architecture

```
Request ──► CLI | Dashboard ──► Orchestrator
                                created → resolving → planned →
                                executing → verifying → completed
                                (awaiting_approval at Approval Gates;
                                 needs_human, failed, limit_reached)
                                   │                      │
                              Tool Layer               Verifier
                        browser / files / mail / erp   reads ground truth
                                   │                      │
                              Mock Suite: MailDesk, LedgerLite, shared files
```

The **Engine** is a guarded state machine; failures resolve in a fixed order:
retry → alternate strategy → re-plan → escalate. **Task Packs** (`tasks/*.yaml`)
declare the tool allowlist, approval rules, and verification contract, so the
engine contains no task-specific logic. The **Company Context** (`company/`)
supplies the SOPs, policies, system registry, and precedents. The **Verifier**
reads LedgerLite state and the filesystem, never the executor's messages or
screenshots. Every Run, verified or not, leaves an **Evidence Pack**.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the state machine, failure
ladder, and trust boundaries.

## Evaluation results

```bash
uv run verirun eval
```

The harness runs twelve seeded scenarios plus two deliberately overclaiming
probes in replay mode — deterministic, offline, and free — and writes
`runs/eval/report.json`.

| Pass | Result |
| --- | --- |
| Baseline | 12/12 known outcomes; 6 completed (two after approval), 6 ended `needs_human`; 47 steps; $0.0365 |
| Verifier off | both overclaiming probes ended `completed` with no verdict |
| Approval gates off | both gated irreversible actions executed with no human decision |

**Caught vs undetected:** the Verifier caught both probe failures (4 failed
checks); without it, both probes ended `completed` with no verification. The
Approval Gates held both irreversible actions; without them, both were submitted.
A policy denial is not an Approval Gate, so the forbidden payment is still
denied with the gates off.

These are mechanism checks, not a benchmark score. The probes are constructed,
the Verifier only catches what a Task Pack contract checks, and replay fixtures
measure the engine rather than live model quality. Full tables and honest
failure cases are under [Evaluation in detail](#evaluation-in-detail).

## The demo

![Terminal recording of verirun demo: a happy path, a transient failure recovered by a retry, an Approval Gate, and three passing Verification contracts](docs/media/demo.gif)

_A real `uv run verirun demo` run, replayed as a terminal recording. The
[WebM version](docs/media/demo.webm) is sharper; regenerate both with
`uv run python scripts/record_demo.py`._

## Sample Evidence Pack

[**Open the sample Evidence Pack**](https://rajprakash00.github.io/verirun/sample-evidence/)
— a completed invoice-processing Run that parked at an Approval Gate, with its
Work Order, Plan, timeline, approval decision, and passing verification checks.
It is one static HTML file with no server and no network: the committed source
is [`docs/sample-evidence/index.html`](docs/sample-evidence/index.html). GitHub
Pages is configured with GitHub Actions as the source and publishes `docs/` on
pushes to `main` that change it
([`pages.yml`](.github/workflows/pages.yml)).

## Run a real request (live model)

```bash
uv run python scripts/seed_mocks.py
uv run verirun run "Process the invoices in the AP mailbox" --task invoice-processing
```

`--task` is required: it selects the Task Pack. The other pack is
`vendor-onboarding`, for example:

```bash
uv run verirun run "Onboard the new supplier from the procurement mailbox" --task vendor-onboarding
```

The CLI prints the Work Order, the Plan, the Verifier's checks, and where the
Evidence Pack was written. A Run that needs a human exits non-zero and parks in
`needs_human` (question) or `awaiting_approval` (approval queue).

A live Run registers the browser tools whenever the Task Pack allows them.
Chromium starts on the first `browser.*` call and writes its screenshots under
`runs/<run-id>/`. When a Plan drives MailDesk or LedgerLite, keep them served —
see the commands under [Dashboard](#dashboard) — so the URLs from
`company/systems.yaml` resolve.

Re-render the Evidence Pack for a finished Run at any time:

```bash
uv run verirun report <run-id>
```

## Dashboard

```bash
uv run verirun serve                        # http://127.0.0.1:8000
```

The dashboard lists Runs and shows the Work Order, the Plan, a live timeline of
steps and tool calls, the approval queue, and the verification results.
Approving an over-limit payment resumes the parked Run; rejecting aborts it with
the recorded reason. A timeout never auto-approves.

Browser Steps in a live `run`, a dashboard-resumed Run, or the demo drive pages
served by these scripts (their URLs match `company/systems.yaml`):

```bash
uv run python scripts/serve_maildesk.py     # http://127.0.0.1:8001
uv run python scripts/serve_ledgerlite.py   # http://127.0.0.1:8002
```

## Evidence Pack

Every Run — verified or not — writes two files under `runs/<run-id>/`:

- `evidence.json` — result, Work Order, Plan, action log, observations,
  Verification checks with evidence references, approvals, escalations, document
  extractions, artifacts, open questions, cost and steps.
- `evidence.html` — a static report generated from the JSON plus the
  screenshots in the Run directory. Images are inlined as data URIs, so the file
  opens standalone in any browser with no server and no network.

## Evaluation in detail

Output on this repository (`uv run verirun eval`):

| Probe | Verifier on | Verifier off |
| --- | --- | --- |
| **Phantom completion** — the doer claims every Step is done without calling a tool | failed: `invoice-filed`, `payment-scheduled`, `source-archived` | `completed`, no verification |
| **Wrong vendor** — every tool call succeeds, but the invoice is filed against the wrong vendor | failed: `invoice-filed` | `completed`, no verification |

| Gated action | Gates on | Gates off |
| --- | --- | --- |
| $12,500 payment above the spend limit | `awaiting_approval`, then completed after a human yes | payment scheduled with no human |
| Vendor creation | `awaiting_approval`, then completed after a human yes | vendor created with no human |
| Forbidden payment to a blocked vendor | `needs_human` with the policy denial | still `needs_human` with the policy denial |

The same harness asserts per-Run metrics: terminal state, verification results
per criterion, human interventions, steps, cost, resume correctness, and
completed actions. It exits non-zero when any scenario misses its known outcome,
so a prompt, model, or Task Pack change is judged in one command. Pass
`--no-browser` to skip the browser re-grounding scenario when Chromium is not
installed, or `--report`, `--state-dir`, `--shared`, `--runs`, and `--fixtures`
to relocate state.

The eval-only ablations are not product switches: `--no-verifier` and
`--no-approval-gates` exist only on `eval`. Neither exists on `run`, `serve`, or
`report`; unset, product behavior is unchanged.

### Honest failure cases

- The two probes are constructed to overclaim; they are not sampled from live
  model traffic. A 2/2 detection rate demonstrates the mechanism, not a
  benchmark score.
- The Verifier only catches what the Task Pack contract checks. A defect outside
  the contract — a plausible but wrong payment date, for example — passes.
- The eval replays committed fixtures, so it measures the engine and Task Packs,
  not live model quality. Changing prompts or the Company Context invalidates
  the fixtures until re-recorded.

Replay fixtures for every baseline and ablation pass are committed under
`evals/fixtures/llm`. After a prompt or Company Context change, refresh them with:

```bash
uv run python scripts/record_eval_fixtures.py
```

## Tests

```bash
uv run pytest
uv run pytest --cov --cov-report=term-missing   # coverage of src/verirun and mocks
uv run ruff check .
```

The suite is offline and deterministic. LLM calls are scripted or replayed from
committed fixtures; no test calls a live model, and the mocks, the run store,
and the Verification checks are all real. Browser tests skip automatically when
Chromium is unavailable.

GitHub Actions runs pytest and Ruff on every push and pull request, and uploads
the coverage report to Codecov.

## The Mock Suite

`scripts/seed_mocks.py` resets and seeds everything:

- **MailDesk** (`mocks/maildesk/`) — a FastAPI webmail with invoice and vendor
  request emails plus attachments.
- **LedgerLite** (`mocks/ledgerlite/`) — a FastAPI ERP with vendors, purchase
  orders, goods receipts, invoices, payments, and approvals, backed by SQLite.
  It is the ground truth the Verifier reads.
- **shared/** — the document tree: `documents/` (sources), `working/` (per-Run
  staging), `archive/` (tax forms), `processed/` (filed invoice sources).
- **SQLite** (`mocks/state/`) — all state; seeded scenarios are fixed, never
  random.

Seeded invoice scenarios: happy path, duplicate invoice, amount mismatch,
missing purchase order, over-limit approval, forbidden action, transient write
failure, and an image-only scanned invoice. Seeded vendor-onboarding scenarios:
a new vendor, a duplicate vendor, and a missing tax form.

## How it works

- **Engine** — a custom state machine. Observe happens inside `executing`: a
  ReAct-style loop runs one Step at a time — think, call one tool, observe.
  Approve is the `awaiting_approval` state the policy gate parks in. Failures
  resolve in a fixed order: retry → alternate strategy → re-plan → escalate.
  The Evidence Pack is written whenever the Run stops, whatever its state.
- **Task Packs** (`tasks/*.yaml`) — the declarative definition of one kind of
  work: goal template, SOP, tool allowlist, approval rules, verification
  contract. The engine contains no task-specific logic.
- **Company Context** (`company/`) — committed SOPs, policies, system registry,
  and precedents. The Work Order resolves ambiguity from it or escalates the gap.
- **Tools** — one uniform interface. The browser is real Playwright; the ERP and
  mail tools read and write the mock systems' SQLite state.
- **Verifier** — reads the Task Pack's contract and checks real state through
  the LedgerLite database and the filesystem. It never reads the executor's
  messages and never trusts a screenshot.
- **Run store** — SQLite: run state, checkpoints, an action journal with
  idempotency keys, observations, and the Evidence Pack index.

### Key technical decisions

Each decision and its trade-offs is recorded as an ADR in
[`docs/adr/`](docs/adr/):

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-vendor-write-tool-and-verifier-check.md) | Generic vendor write tool and vendor verification check, so a second Task Pack needs no engine change. |
| [0002](docs/adr/0002-custom-engine-over-a-framework.md) | A custom state machine over an agent framework: the escalation ladder, approval gates, idempotent journal, and verification phase are first-class. |
| [0003](docs/adr/0003-accessibility-tree-browser-control.md) | Accessibility-tree snapshots with stable refs over vision-first browser control; vision is reserved for what the tree cannot serve. |
| [0004](docs/adr/0004-replay-brain-for-deterministic-tests.md) | A provider-agnostic client with record/replay fixtures and a scripted test client, so tests are deterministic, offline, and free. |
| [0005](docs/adr/0005-browser-regrounding.md) | Browser re-grounding: a stale ref is re-resolved by role and name, a control the tree lacks goes to the vision fallback, and the locator used is journaled. |
| [0006](docs/adr/0006-eval-only-ablations.md) | Eval-only switches disable the Verifier or the Approval Gates; the harness reports caught vs undetected failures and gated vs ungated irreversible actions. |

### Models and APIs

The LLM layer is an OpenAI-compatible chat-completions client configured
entirely from `.env` (see [`.env.example`](.env.example)). No other network API
is called at runtime; MailDesk and LedgerLite run locally.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VERIRUN_BASE_URL` | `https://opencode.ai/zen/go/v1` | Any OpenAI-compatible gateway. |
| `VERIRUN_API_KEY` | empty | Required for `live` and `record` modes. |
| `VERIRUN_MODEL_LOOP` | `deepseek-v4.1-flash` | Fast model for the Execute tool loop. |
| `VERIRUN_MODEL_REASON` | `deepseek-v4-pro` | Stronger model for Resolve, Plan, and Verify. |
| `VERIRUN_MODEL_VISION` | `deepseek-v4-flash-vision-exp` | Reads image-only scanned documents. |
| `VERIRUN_LLM_MODE` | `live` | `live`, `record`, or `replay`. |
| `VERIRUN_FIXTURE_DIR` | `tests/fixtures/llm` | Recorded responses keyed by request hash. |
| `VERIRUN_MAX_STEPS` | `60` | Per-run step meter. |
| `VERIRUN_MAX_COST_USD` | `5.0` | Per-run cost meter. |
| `VERIRUN_REQUEST_TIMEOUT_S` | `600` | Per-request model timeout; transient timeouts are retried. |

Prompt caching is enabled by sending a stable per-run session id header. Model
prices live in `src/verirun/config.py` and drive the cost meter.
`replay` mode serves recorded fixtures offline; `record` mode
(`uv run python scripts/record_llm_fixtures.py`) refreshes them when prompts or
the Company Context change.

### Assumptions

- The sandbox has no real third-party systems: MailDesk, LedgerLite, and the
  shared file tree stand in for the mailbox, ERP, and document store.
- Payment is scheduling, not transfer.
- Requests are short and may be incomplete; the Company Context resolves what it
  can, and anything left is an open question, not a guess.
- Verification reads ground truth (ERP records and files), never the executor's
  messages or screenshots.
- One Run handles one Request; the two Task Packs are the demonstrated scope.
- A human is available to answer approval gates and escalations.

## Known limitations

What is not built:

- **Real external systems.** MailDesk, LedgerLite, and the shared file tree are
  mocks. Payments are scheduled, not transferred; no email is sent; no network
  writes happen outside the local Mock Suite.
- **Authentication, authorization, multi-tenancy, or cloud deployment.** The
  dashboard binds to localhost and has no login or tenant boundary.
- **A queue, scheduler, or parallel execution.** One process runs one Request at
  a time; there is no worker pool, cron, or cross-process supervisor.
- **Task coverage beyond the two Task Packs** (`invoice-processing` and
  `vendor-onboarding`). New work means a new Task Pack, not a general-purpose
  agent.
- **Learning or long-term memory.** Precedents are committed documents; a Run
  never writes one.
- **A fallback LLM provider.** There is one OpenAI-compatible chat-completions
  client, no provider failover.
- **Native desktop GUI automation.** Only the browser is driven through
  Playwright, plus the file, mail, and ERP tools. Canvas-only or unlabeled
  widgets have no reliable path; the vision fallback is best-effort.
- **A published package or container image.** Run from a checkout; there is no
  PyPI release or Docker image.
- **A public benchmark or leaderboard.** The eval is a committed harness over
  twelve seeded scenarios, not a benchmark of model quality.
- **A live trace viewer.** `evidence.html` is a snapshot of a Run directory;
  screenshots are inlined as recorded.
- **Defects outside the verification contract.** The Verifier only checks the
  Task Pack's declared criteria; a plausible but wrong payment date, for
  example, passes.
- **Stable replay across prompt changes.** Fixtures are keyed by a hash of the
  exact request; changing the Company Context or prompts invalidates them until
  re-recorded.

## What I'd build next

With two more weeks, in priority order:

1. **A company-memory write path.** After each verified Run, propose a
   structured precedent (vendor quirks, working selectors, mismatch
   resolutions). A human approves it. Resolve and Plan then retrieve the top few
   precedents per Request. Memory stays static until this path exists.
2. **A trace-to-Task-Pack compiler.** Draft a Task Pack from a verified Run's
   journal: goal template, tool allowlist, approval rules, verification
   contract. A human reviews the draft. This lowers a new kind of work from a
   hand-written pack and check to a review.
3. **Production hardening, after the above.** A Postgres-backed queue with
   scheduling, authentication and tenancy, a secrets vault, per-tool capability
   scopes, and OpenTelemetry audit export.

## Repository layout

```
src/verirun/
├── demo.py        # the one-command demo (also scripts/demo.py)
├── engine/        # state machine, planner, executor, verifier, adaptation
├── context/       # Company Context loader, Work Order, Task Packs
├── llm/           # OpenAI-compatible client, replay, cost meter
├── tools/         # browser, files, mail, erp, policy gate
├── runs/          # run store, evidence pack, evidence HTML
└── web/           # dashboard and approval API
mocks/             # MailDesk, LedgerLite, seed data
company/           # SOPs, policies, system registry, precedents
tasks/             # Task Packs (YAML)
scripts/           # seed, serve, fixture recording, demo recording
tests/             # integration, acceptance, and unit tests
docs/              # architecture, ADRs, sample Evidence Pack, recording
```

## Contributing

Setup, test, and pull request rules live in [`CONTRIBUTING.md`](CONTRIBUTING.md).
Report vulnerabilities privately per [`SECURITY.md`](SECURITY.md); release notes
are in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).
