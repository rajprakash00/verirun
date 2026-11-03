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

One unchanged engine runs two Mock Suite Task Packs — invoice processing and
vendor onboarding — and a Phase 1 India AP pack that extracts GST fields and
creates a Zoho Books purchase bill after any required approval. The demo is
offline, deterministic, and needs no API key.

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
                              Adapters: MailSource + AccountingSystem
                                   │                      │
                              Mock Suite: MailDesk, LedgerLite, shared files
```

The **Engine** is a guarded state machine; failures resolve in a fixed order:
retry → alternate strategy → re-plan → escalate. **Task Packs** (`tasks/*.yaml`)
declare the tool allowlist, approval rules, and verification contract, so the
engine contains no task-specific logic. The **Company Context** (`company/`)
supplies the SOPs, policies, system registry, and precedents. The **Verifier**
reads the system of record through a read-only adapter and the filesystem,
never the executor's messages or screenshots. Every Run, verified or not,
leaves an **Evidence Pack**.

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

Start a worker, then enqueue a request:

```bash
uv run python scripts/seed_mocks.py
uv run verirun worker                         # terminal 1
uv run verirun run "Process the invoices in the AP mailbox" --task invoice-processing
```

`verirun run` creates the Run and queues a `start` job; the worker claims it and
executes it. Add `--wait` to run the queue in the same process when you do not
want a second terminal. `--task` is required: it selects the Task Pack. The other
pack is `vendor-onboarding`, for example:

```bash
uv run verirun run "Onboard the new supplier from the procurement mailbox" --task vendor-onboarding
```

The worker can run with `--once` for a single job, `--max-items N`, or
`--lease-seconds`. A Run that needs a human rests in `needs_human` (question) or
`awaiting_approval` (approval queue); approving or rejecting it in the dashboard
queues a `resume` job for the worker.

Set `VERIRUN_DATABASE_URL` to a PostgreSQL DSN to use the product store and
share the queue across worker processes. Without it, the SQLite store keeps the
demo and tests zero-dependency.

With `--wait`, the CLI prints the Work Order, the Plan, the Verifier's checks, and
where the Evidence Pack was written; a Run that needs a human exits non-zero and
parks in `needs_human` (question) or `awaiting_approval` (approval queue).

A live Run registers the browser tools whenever the Task Pack allows them.
Chromium starts on the first `browser.*` call and writes its screenshots under
`runs/<run-id>/`. When a Plan drives MailDesk or LedgerLite, keep them served —
see the commands under [Dashboard](#dashboard) — so the URLs from
`company/systems.yaml` resolve.

Re-render the Evidence Pack for a finished Run at any time:

```bash
uv run verirun report <run-id>
```

### India GST mail sources (Phase 1)

`verirun run` reads the Mock Suite by default. Set one of these to read a real
mailbox instead; the CLI syncs it before the Run:

| Setting | Purpose |
| --- | --- |
| `VERIRUN_IMAP_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_FOLDER`, `_SSL` | IMAP over SSL with an app password. |
| `VERIRUN_WATCH_DIR` | A folder of invoice PDFs, no mailbox access. |
| `VERIRUN_MAIL_STATE_DB` | Local UIDVALIDITY/UID and message index (default `runs/mail-index.db`). |

Attachments are stored content-addressed under
`VERIRUN_SHARED_DIR/attachments/`; reconnecting or a UIDVALIDITY change does not
reprocess an ingested message. `invoice.extract` reads an India GST text-layer
PDF with a confidence and citation per field, and records GSTIN and tax
validations in the Evidence Pack. The India Task Pack's Verifier matches the
extraction to the accounting system's vendor, duplicate bills, and purchase
orders, then checks the created bill against the source PDF: existence, vendor
and GSTIN, invoice number, amount, tax arithmetic, purchase order, and source
document. The India policy parks bills above 10,000 INR, unmatched vendors,
missing required purchase orders, and low-confidence extractions at an Approval
Gate. An inactive vendor is a policy denial, not a gate.

### Zoho Books write path (Phase 1)

Setting the Zoho Books settings below swaps the accounting adapter from
LedgerLite to Zoho Books India. The Verifier keeps a read-only
`AccountingReads` object; the executor gets a separate write token and creates
exactly one purchase bill after any required approval. The idempotency key is
journaled before the call and written into the bill's reference number, so a
retry reconciles the earlier write instead of creating a duplicate. A
network or 5xx outcome that cannot be reconciled parks for a human; the
adapter never blindly retries a write.

| Setting | Purpose |
| --- | --- |
| `VERIRUN_ZOHO_ORGANIZATION_ID` | The Zoho Books India organisation id. |
| `VERIRUN_ZOHO_CLIENT_ID`, `_CLIENT_SECRET` | A Self Client from the Zoho API console (India DC). |
| `VERIRUN_ZOHO_READ_REFRESH_TOKEN` | A read-only refresh token with `ZohoBooks.contacts.READ`, `ZohoBooks.purchaseorders.READ`, `ZohoBooks.bills.READ`, and `ZohoBooks.vendorpayments.READ`. |
| `VERIRUN_ZOHO_WRITE_REFRESH_TOKEN` | A separate write refresh token with the matching `.CREATE` scope plus the reads the executor needs. |
| `VERIRUN_ZOHO_TOKEN_KEY` | Operator secret used to encrypt both token files at rest. |
| `VERIRUN_ZOHO_READ_TOKEN_FILE`, `_WRITE_TOKEN_FILE` | Encrypted token caches (default `runs/zoho-read-token.enc` and `runs/zoho-write-token.enc`). |
| `VERIRUN_ZOHO_ACCOUNTS_DOMAIN`, `_API_BASE_URL` | Default `https://accounts.zoho.in` and `https://www.zohoapis.in/books/v3`. |

Zoho creates each refresh token once; Verirun refreshes the short-lived access
tokens and writes them to the encrypted files. If a file exists, its refresh
token can be removed from the environment. A partial Zoho configuration is an
error rather than a silent fallback to LedgerLite.

The recorded-fixture contract lives in `tests/test_zoho_client.py`,
`tests/test_zoho_reads.py`, and `tests/test_zoho_e2e.py`. To check a real trial
before a pilot, export the same settings and run:

```bash
VERIRUN_ZOHO_LIVE=1 uv run pytest tests/test_zoho_live.py
```

To run the match-and-create path end to end against the configured trial, keep a
worker running and enqueue the Run:

```bash
uv run verirun worker
uv run verirun run "Process the GST invoice in the AP mailbox" \
  --task invoice-processing-in
```


## Dashboard

```bash
uv run verirun serve                        # http://127.0.0.1:8000
```

The dashboard lists Runs and shows the Work Order, the Plan, a live timeline of
steps and tool calls, the approval queue, and the verification results. The
approval review shows the source invoice, the extraction with citations, the
GST validations, and the proposed purchase bill.
Approving an over-limit payment or bill queues a `resume` job for the worker;
rejecting aborts the Run with the recorded reason. A timeout never auto-approves.
The Exception page shows any Run whose write outcome needs reconciliation.

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
  mail tools talk to `AccountingSystem` and `MailSource` adapters, implemented
  by the Mock Suite, the in-memory fake, an IMAP source, and a watched folder.
- **Verifier** — reads the Task Pack's contract and checks real state through
  the read-only `AccountingReads` adapter and the filesystem. It never reads
  the executor's messages and never trusts a screenshot.
- **Run store** — SQLite by default; PostgreSQL when `VERIRUN_DATABASE_URL` is
  set. It holds run state, checkpoints, the outbox journal with idempotency keys
  and natural-key claims, the leased work queue, Exceptions, observations, and
  the Evidence Pack index. The CLI and dashboard enqueue work; `verirun worker`
  claims and executes it.

### Durable execution

The journal is the outbox: an `intent` and its natural-key claim are committed
before an external call, and `done` after it. A crash between the two is
reconciled on resume by looking for the bill that carries the Run's idempotency
marker. A write whose outcome cannot be established becomes `unknown`, parks the
Run on an Exception with the vendor, invoice number, journal key, and error, and
is never retried automatically. A purchase bill is claimed once per vendor and
invoice number across every Run.

The worker leases jobs. SQLite claims under a write transaction; PostgreSQL uses
`FOR UPDATE SKIP LOCKED`, renews the lease while running, and reclaims an expired
lease after a crash. Exhausted jobs move to a dead-letter state.
`tests/test_crash_safety.py` kills the worker before intent, after intent before
the call, after the call before `done`, and after `done`; every resume ends with
exactly one bill, and the unknown-outcome case leaves an Exception. Run that
demo with:

```bash
uv run pytest tests/test_crash_safety.py -q
```

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
| [0007](docs/adr/0007-mail-and-accounting-adapters.md) | `MailSource` and `AccountingSystem` adapters separate the engine from MailDesk and LedgerLite; the Verifier gets read-only accounting access, and an in-memory fake backs engine tests. |
| [0008](docs/adr/0008-india-gst-extraction.md) | IMAP and watched-folder sources ingest into a content-addressed index; India GST extraction returns a confidence and a verified citation per field, with deterministic GSTIN and tax validations in the Evidence Pack. |
| [0009](docs/adr/0009-zoho-read-only-verifier.md) | Zoho Books reads use an encrypted OAuth token; the Verifier gets a separate read-only object and pre-write vendor, duplicate, and purchase-order matching. |
| [0010](docs/adr/0010-zoho-write-and-approval.md) | Purchase bills are created through a separate write token, journaled idempotency keys, and reconcile-before-retry; INR, vendor, purchase-order, and confidence facts drive the Approval Gate, and the Verifier re-reads the source PDF. |
| [0011](docs/adr/0011-durable-execution.md) | PostgreSQL and SQLite share one run store; a leased worker claims queued Runs, the journal is an outbox with `unknown`, natural-key claims stop duplicate bills, and Exceptions carry reconciliation evidence. |

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
| `VERIRUN_DATABASE_URL` | empty | PostgreSQL DSN for the product run store and worker queue; empty uses SQLite. |

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
- One Run handles one Request; the Task Packs are the demonstrated scope.
- A human is available to answer approval gates and escalations.

## Known limitations

What is not built:

- **Live Zoho Books and IMAP trials.** The product path is tested against
  recorded Zoho Books responses and a local IMAP fixture, not a live trial in
  CI. It ingests an AP mailbox over IMAP or a watched folder, extracts India GST
  fields with validations, matches the vendor and purchase order through a
  read-only token, creates one purchase bill through a separate write token
  after any required approval, verifies the bill against the source PDF, and
  archives the source. MailDesk, LedgerLite, and the shared file tree remain the
  offline fixtures; payments are scheduled, not transferred; no email is sent.
- **Zoho GST tax lines.** The source invoice's GSTIN and tax arithmetic are
  verified, and the Zoho bill total must match, but bill creation posts one line
  item without an organisation-specific `tax_id`. Populating Zoho tax lines is a
  later integration with the customer's tax setup.
- **Authentication, authorization, multi-tenancy, or cloud deployment.** The
  dashboard binds to localhost and has no login or tenant boundary.
- **Parallel execution and scheduling.** The worker processes one job at a time
  and has no cron, priority classes, or per-tenant pools. Multiple workers share
  the PostgreSQL queue, but one install still serves one company.
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
3. **Production hardening, after the above.** Authentication and tenancy, a
   secrets vault, per-tool capability scopes, scheduling and priority classes on
   the worker queue, and OpenTelemetry audit export.

## Repository layout

```
src/verirun/
├── demo.py        # the one-command demo (also scripts/demo.py)
├── adapters/      # MailSource + AccountingSystem seams, IMAP, folder, fakes
├── engine/        # state machine, planner, executor, verifier, adaptation
├── context/       # Company Context loader, Work Order, Task Packs
├── india/         # India GST schema, GSTIN and tax validations
├── llm/           # OpenAI-compatible client, replay, cost meter
├── tools/         # browser, files, mail, erp, invoice extraction, policy gate
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
