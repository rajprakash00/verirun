# Operator

Operator turns a short company Request into completed work. For every Request it:

1. reads the **Company Context** (SOPs, policies, system registry, precedents) to
   discover the unstated steps;
2. writes a **Work Order**: assumptions, steps, systems, approval gates, and
   success criteria;
3. **plans and executes** with real tools (browser, files, mail, internal
   systems);
4. **observes and adapts** on failure: retry, alternate strategy, re-plan, or
   escalate;
5. stops at **Approval Gates** before irreversible actions;
6. **verifies** the outcome independently against real system state — not
   against its own messages; and
7. returns an **Evidence Pack**: result, proof, action log, open questions.

One unchanged engine runs two tasks, demonstrated against a self-contained Mock
Suite: **invoice processing** and **vendor onboarding**.

- Product spec: [`SPEC.md`](SPEC.md)
- Design document: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Glossary: [`CONTEXT.md`](CONTEXT.md)

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Headless Chromium for the browser tools and browser tests (Playwright
  downloads its own copy)

## Setup

```bash
uv sync
uv run playwright install chromium          # add --with-deps on a fresh Linux box
cp .env.example .env                        # fill in OPERATOR_API_KEY for live runs
```

The demo and the test suite run offline and need no API key. Live model calls
happen only when you start a request (`operator run`) or approve a parked Run in
the dashboard; both use the gateway settings from `.env`.

## Run the demo

```bash
uv run python scripts/demo.py
```

The script seeds the Mock Suite and runs three scenarios end to end on the
unchanged engine:

1. **Happy path** — read the invoice email, validate against the PO and goods
   receipt, file it, schedule the payment, and archive the source. (With
   Chromium installed, it also captures a LedgerLite screenshot.)
2. **Transient failure** — the ERP write fails once and the engine retries it.
3. **Over-limit approval** — the Run parks at the payment gate, a human
   approves, and the resumed Run completes.

Every scenario prints its state and leaves both an `evidence.json` and a
standalone `evidence.html` under `runs/<run-id>/`. Pass `--no-browser` to skip
the screenshot, or `--state-dir`, `--shared`, and `--runs` to relocate state.

## Run a real request (live model)

```bash
uv run python scripts/seed_mocks.py
uv run operator run "Process the invoices in the AP mailbox" --task invoice-processing
```

`--task` is required: it selects the Task Pack. The other pack is
`vendor-onboarding`, for example:

```bash
uv run operator run "Onboard the new supplier from the procurement mailbox" --task vendor-onboarding
```

The CLI prints the Work Order, the Plan, the Verifier's checks, and where the
Evidence Pack was written. A Run that needs a human exits non-zero and parks in
`needs_human` (question) or `awaiting_approval` (approval queue).

Re-render the Evidence Pack for a finished Run at any time:

```bash
uv run operator report <run-id>
```

## Dashboard

```bash
uv run operator serve                       # http://127.0.0.1:8000
```

The dashboard lists Runs and shows the Work Order, the Plan, a live timeline of
steps and tool calls, the approval queue, and the verification results.
Approving an over-limit payment resumes the parked Run; rejecting aborts it with
the recorded reason. A timeout never auto-approves.

To watch the browser tools work against the mocks, serve them too (their URLs
match `company/systems.yaml`):

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

## Tests

```bash
uv run pytest
uv run ruff check .
```

The suite is offline and deterministic. LLM calls are scripted in tests; the
mocks, the run store, and the Verification checks are all real. Browser tests
skip automatically when Chromium is unavailable.

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

Seeded scenarios: happy path, duplicate invoice, amount mismatch, missing
purchase order, over-limit approval, forbidden action, transient write failure,
and an image-only scanned invoice.

## Architecture in one minute

```
Request ──► CLI | Dashboard ──► Orchestrator
                                Resolve → Plan → Approve →
                                Execute → Observe → Verify → Report
                                   │                      │
                              Tool Layer               Verifier
                        browser / files / mail / erp   reads ground truth
                                   │                      │
                              Mock Suite: MailDesk, LedgerLite, shared files
```

- **Engine** — a custom state machine. Inside Execute, a ReAct-style loop runs
  one Step at a time: think, call one tool, observe. Failures resolve in a fixed
  order: retry → alternate strategy → re-plan → escalate.
- **Task Packs** (`tasks/*.yaml`) — the declarative definition of one kind of
  work: goal template, SOP, tool allowlist, approval rules, verification
  contract. The engine contains no task-specific logic.
- **Company Context** (`company/`) — committed SOPs, policies, system registry,
  and precedents. The Work Order resolves ambiguity from it or escalates the gap.
- **Tools** — one uniform interface. The browser is real Playwright; the ERP and
  mail tools read and write the mock systems' SQLite state.
- **Verifier** — reads the Task Pack's contract and checks real state through
  the LedgerLite API/database and the filesystem. It never reads the executor's
  messages and never trusts a screenshot.
- **Run store** — SQLite: run state, checkpoints, an action journal with
  idempotency keys, observations, and the Evidence Pack index.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

## Key technical decisions

Each decision and its trade-offs is recorded as an ADR in
[`docs/adr/`](docs/adr/):

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-vendor-write-tool-and-verifier-check.md) | Generic vendor write tool and vendor verification check, so a second Task Pack needs no engine change. |
| [0002](docs/adr/0002-custom-engine-over-a-framework.md) | A custom state machine over an agent framework: the escalation ladder, approval gates, idempotent journal, and verification phase are first-class. |
| [0003](docs/adr/0003-accessibility-tree-browser-control.md) | Accessibility-tree snapshots with stable refs over vision-first browser control; vision is reserved for scanned documents. |
| [0004](docs/adr/0004-replay-brain-for-deterministic-tests.md) | A provider-agnostic client with record/replay fixtures and a scripted test client, so tests are deterministic, offline, and free. |

## Models and APIs

The LLM layer is an OpenAI-compatible chat-completions client configured
entirely from `.env` (see [`.env.example`](.env.example)). No other network API
is called at runtime; MailDesk and LedgerLite run locally.

| Setting | Default | Purpose |
| --- | --- | --- |
| `OPERATOR_BASE_URL` | `https://opencode.ai/zen/go/v1` | Any OpenAI-compatible gateway. |
| `OPERATOR_API_KEY` | empty | Required for `live` and `record` modes. |
| `OPERATOR_MODEL_LOOP` | `deepseek-v4.1-flash` | Fast model for the Execute tool loop. |
| `OPERATOR_MODEL_REASON` | `deepseek-v4-pro` | Stronger model for Resolve, Plan, and Verify. |
| `OPERATOR_MODEL_VISION` | `deepseek-v4-flash-vision-exp` | Reads image-only scanned documents. |
| `OPERATOR_LLM_MODE` | `live` | `live`, `record`, or `replay`. |
| `OPERATOR_FIXTURE_DIR` | `tests/fixtures/llm` | Recorded responses keyed by request hash. |
| `OPERATOR_MAX_STEPS` | `60` | Per-run step meter. |
| `OPERATOR_MAX_COST_USD` | `5.0` | Per-run cost meter. |
| `OPERATOR_REQUEST_TIMEOUT_S` | `600` | Per-request model timeout; transient timeouts are retried. |

Prompt caching is enabled by sending a stable per-run session id header. Model
prices live in `src/company_operator/config.py` and drive the cost meter.
`replay` mode serves recorded fixtures offline; `record` mode
(`uv run python scripts/record_llm_fixtures.py`) refreshes them when prompts or
the Company Context change.

## Assumptions

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

- No native desktop GUI control; only the browser is driven, and only through
  the accessibility tree and file tools.
- No real external systems, credentials, payments, email sending, or network
  writes — all effects are simulated in the Mock Suite.
- No authentication, multi-tenancy, or cloud deployment; the dashboard binds to
  localhost.
- Task coverage is limited to the two demonstrated Task Packs.
- No learning or long-term memory beyond committed precedent documents.
- Replay fixtures are keyed by a hash of the exact request; changing the
  Company Context or prompts invalidates them until re-recorded.
- The static `evidence.html` is a snapshot of a Run directory; screenshots are
  inlined as recorded.

## Repository layout

```
src/company_operator/
├── engine/        # state machine, planner, executor, verifier, adaptation
├── context/       # Company Context loader, Work Order, Task Packs
├── llm/           # OpenAI-compatible client, replay, cost meter
├── tools/         # browser, files, mail, erp, policy gate
├── runs/          # run store, evidence pack, evidence HTML
└── web/           # dashboard and approval API
mocks/             # MailDesk, LedgerLite, seed data
company/           # SOPs, policies, system registry, precedents
tasks/             # Task Packs (YAML)
scripts/           # seed, serve, demo, fixture recording
tests/             # integration, acceptance, and unit tests
docs/              # architecture and ADRs
```
