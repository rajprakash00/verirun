# Architecture (High-Level Design)

Verirun turns a short Request into completed work: it plans, executes with real tools,
adapts on failure, stops for human approval, verifies against ground truth, and returns
an Evidence Pack.

## 1. Component overview

```
                        ┌──────────────────────────────┐
 Request ──────────────►│   CLI  |  Dashboard (FastAPI) │◄──── approval decisions
                        └───────────────┬──────────────┘
                                        │
                        ┌───────────────▼──────────────┐
                        │        Run Orchestrator       │
                        │  created → resolving →        │
                        │  planned → executing →        │
                        │  verifying → completed        │
                        │  (plus awaiting_approval,     │
                        │  needs_human, failed,         │
                        │  limit_reached)               │
                        └───┬──────────────────┬────────┘
                            │                  │
              ┌──────────────▼─────┐   ┌────────▼──────────┐
              │     Tool Layer     │   │      Verifier     │
              │ browser / files /  │   │  reads the system │
              │     mail / erp     │   │ of record directly│
              └──────────┬─────────┘   └────────┬──────────┘
                         │                      │
              ┌──────────▼──────────────────────▼──────────┐
              │           Adapters (interfaces)            │
              │     MailSource        AccountingSystem     │
              │      AccountingReads (Verifier only)       │
              └──────────┬──────────────────────┬──────────┘
                         │                      │
              ┌──────────▼──────────────────────▼──────────┐
              │                 Mock Suite                 │
              │   MailDesk (webmail)   LedgerLite (ERP)    │
              │     shared/ file tree    SQLite state      │
              └────────────────────────────────────────────┘

 Company Context (SOPs, policies, registry) ──► Orchestrator
 Task Packs (YAML)                          ──► Orchestrator
 LLM client (live | record | replay)        ──► Orchestrator + Verifier
```

The engine and the Verifier depend on adapters, never on a mock database.
`MailSource` covers listing, reading, and opening messages and attachments.
`AccountingReads` covers vendor, purchase-order, goods-receipt, bill, payment,
and pending-approval reads; the Verifier gets only this interface.
`AccountingSystem` adds purchase-bill creation plus the Mock Suite's vendor
creation and payment scheduling. The Mock Suite adapters, the in-memory fake,
an IMAP source, and a watched-folder source implement them; Zoho Books will be
the next accounting swap (ADR 0007, ADR 0008).

India GST invoices go through `invoice.extract`: the model returns every field
with a confidence and an exact citation, and deterministic GSTIN and tax
validations run on the normalised paise values. A citation that is not in the
document, or does not contain its value, fails the extraction; a failed GST
validation is recorded in the Evidence Pack.

## 2. Engine

The Run is a guarded state machine (`src/verirun/engine/states.py`). Every
transition is persisted in the run store.

| State | What happens |
| --- | --- |
| `created` | The Run exists; nothing has happened yet. |
| `resolving` | Resolve and Plan: the Work Order is written from the Request and Company Context, then the Plan from the Work Order and the Task Pack. A question the context cannot answer parks the Run in `needs_human`. |
| `planned` | The Plan is ready to execute. |
| `executing` | The ReAct tool loop: one Step at a time — think, call one Tool, Observe the result. |
| `awaiting_approval` | The policy gate prepared an irreversible action and parked the Run; it resumes only on a human yes/no. |
| `verifying` | The Verifier checks the outcome against ground truth. |
| `completed` | Every verification criterion passed. Terminal. |
| `needs_human` | An escalation with one question; a human answer resumes the Run. |
| `failed` | A rejected approval or an unrecoverable error. Resumable where a transition allows. |
| `limit_reached` | The step or cost meter stopped the Run. Terminal. |

The allowed transitions:

```
created          → resolving | failed
resolving        → planned | needs_human | failed
planned          → awaiting_approval | executing | needs_human | failed
awaiting_approval → executing | needs_human | failed
executing        → awaiting_approval | verifying | needs_human | failed | limit_reached
verifying        → completed | needs_human | failed
failed           → resolving | executing
needs_human      → planned | executing
```

`completed` and `limit_reached` are terminal. Three names from the product
vocabulary are not states: Observe happens inside `executing` (every Tool call
records an Observation), Approve is the `awaiting_approval` state entered from
inside `executing`, and Report is not a state at all — `run_task` and
`resume_run` write the Evidence Pack before returning, whatever state the Run
is in.

Failure escalation order is fixed: **retry → alternate strategy → re-plan → escalate**.
A single orchestrator runs the loop; the Verifier is a separate phase, not a second worker.

### Failure adaptation

Every tool call is recorded as an Observation with its attempt number, so every
retry stays visible. Failures resolve in a fixed order:

1. **Retry** — transient failures are retried automatically, twice.
2. **Alternate strategy** — the model sees the failure and may try another tool
   or another argument.
3. **Re-plan** — after three failed attempts on one Step, the engine asks the
   planner for a revised plan, once, with the failure context (including the
   side effects that already succeeded).
4. **Escalate** — the Run parks in `needs_human` with an open question, and keeps
   its plan, journal, and checkpoints so a human can resume it.

Business conditions that retrying cannot fix skip the ladder. The seeded
scenarios and their terminal states:

| Scenario | Terminal state | Recorded reason |
| --- | --- | --- |
| Happy path | `completed` | (none; every verification criterion passes) |
| Transient write failure | `completed` after one retry | (none; the retry succeeds) |
| Scanned invoice | `completed` via the vision path | (none; extraction is above the confidence threshold) |
| Over-limit approval | `awaiting_approval`, then `completed` after a human approves | (none; the gate waits) |
| Duplicate invoice | `needs_human` | the existing invoice id and status |
| Amount mismatch | `needs_human` | invoice amount vs purchase order or goods receipt amount |
| Missing purchase order | `needs_human` | the model's question asking for the PO |
| Forbidden action | `needs_human` | the policy id and rule that denied the action |
| Vendor onboarding | `awaiting_approval`, then `completed` after a human approves | (none; the gate waits) |
| Duplicate vendor | `needs_human` | the existing vendor id and status |
| Missing tax form | `needs_human` | the model's question asking for the form |

The model can also escalate explicitly with the engine-owned `task.escalate`
tool when it finds a blocker no tool can resolve, such as a missing purchase
order. The per-run step and cost meters bound the worst case: no scenario loops.

## 3. Company Context and the Work Order

`company/` is committed knowledge: SOPs (Markdown), policies (YAML: spend limits,
permitted/forbidden actions), a system registry (URLs, sandbox credentials), and
precedents. The Work Order is Verirun's written interpretation of a Request and is
shown in the dashboard before execution.

## 4. Task Packs

One declarative YAML file per kind of work: goal template, SOP reference, tool allowlist,
policy references, approval rules, and a verification contract. The engine never contains
task-specific logic; swapping the Task Pack swaps the task. Demonstrated with
`invoice-processing`, `vendor-onboarding`, and the Phase 1 India extraction pack
`invoice-processing-in`.

## 5. Mock Suite

- **MailDesk**: webmail UI with seeded invoice and vendor emails plus attachments.
  `MockMailSource` exposes it through the `MailSource` adapter.
- **LedgerLite**: ERP UI with vendors, purchase orders, goods receipts, invoices, payments,
  and approvals; exposes an HTTP API for its UI and the tests. `MockAccountingSystem`
  exposes it through the `AccountingSystem` adapter, and the Verifier reads it through
  `AccountingReads`.
- **shared/**: documents (PDFs, scans, tax forms) and archive folders.
- **SQLite**: system state; seed scripts create all scenarios. Only the adapters open it.

### Product mail sources

- **IMAP**: host, port, username, and app password over SSL by default. The
  adapter stores each folder's UIDVALIDITY and last ingested UID, fetches only
  newer UIDs, and rescans safely when the server starts a new UIDVALIDITY epoch.
- **Watched folder**: each PDF dropped into a directory becomes one message.
- Both ingest into a local SQLite index and a content-addressed store
  (`shared/attachments/<sha-prefix>/<sha>/<name>`). The message id is derived
  from the source bytes, so a reconnect or a re-sync cannot store the same
  message twice. The local index is authoritative; the engine never depends on
  the server's `\Seen` flag.
- **India GST extraction**: `invoice.extract` reads a text-layer PDF, asks the
  reasoning model for the GST fields under a strict schema, checks every
  citation against the document, normalises amounts to paise and dates to ISO,
  and records GSTIN and tax validations. Scans still use the US vision schema
  until the India Task Pack defines scan scenarios.

## 6. Human-in-the-loop

Approval Gates are declared per Task Pack. Irreversible actions (schedule payment above
limit, create vendor) may be prepared but not submitted. Decisions
arrive through the dashboard approval queue. Timeouts never auto-approve. Ambiguity or
repeated failure escalates with a question.

## 7. Verification

The Verifier reads the Task Pack's verification contract and checks real state through the
read-only accounting adapter (`AccountingReads`) and the filesystem. It does not read the
executor's messages and does not trust screenshots. Output: pass/fail per criterion with
evidence references. Screenshots are display evidence only.

The eval harness has two eval-only switches that the product paths leave at their defaults.
Disabling the Verifier accepts the executor's outcome without a verification pass and writes
no Evidence Pack for that Run; disabling the Approval Gates lets a `require_approval` policy
decision execute, while `forbid` decisions still deny. The harness runs both ablations beside
the baseline and reports caught failures, undetected failures, and gated versus ungated
irreversible actions. See ADR 0006.

## 8. Reliability and state

- Run store (SQLite): run state, checkpoints, observations, Evidence Pack index.
- Action journal with idempotency keys: retries never repeat completed side effects.
- Browser re-grounding: a stale ref is re-resolved against a fresh snapshot by role
  and accessible name; a control the accessibility tree lacks is located by a
  screenshot and the vision model. The locator an action used is recorded in the
  Observation, and therefore in the journal, so a retry replays it.
- Checkpointing: a failed run resumes from its last checkpoint.
- Step and cost meters per run; the run stops at its limit.
- Failures are scenario-driven and seeded, never random.

## 9. LLM layer and cost control

- OpenAI-compatible client with a provider base URL and model roles in configuration.
- Roles: fast tool-loop model, stronger plan/verify model, vision model for scans.
- Replay mode serves recorded fixtures for deterministic, offline, zero-cost tests.
- Stable per-run session ID enables prompt caching.
- Live and record modes require `VERIRUN_API_KEY`; replay, the demo, and the test
  suite need no key.

## 10. Tech stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.12+ with `uv` |
| Data models | Pydantic |
| Browser | Playwright (accessibility-tree refs) |
| Mock apps | FastAPI + SQLite + Jinja templates |
| Dashboard | FastAPI + lightweight HTML/JS |
| PDFs | pypdf for text, pypdfium2 page render + vision model for scans |
| Files | pathlib; fpdf2 generates the seeded mock documents |
| Tests | pytest, replay LLM fixtures |
| Lint/format | Ruff |
| Evidence | JSON + generated static HTML |

## 11. Folder structure

```
verirun/
├── AGENTS.md                  # agent working agreements
├── CONTEXT.md                 # domain glossary
├── README.md                  # setup, run, architecture, decisions, limits
├── pyproject.toml
├── .env.example
├── company/                   # Company Context pack
│   ├── sops/
│   ├── policies/
│   ├── systems.yaml
│   └── precedents/
├── tasks/                     # Task Packs (YAML)
│   ├── invoice-processing.yaml
│   ├── invoice-processing-in.yaml
│   └── vendor-onboarding.yaml
├── src/verirun/
│   ├── demo.py                # one-command demo
│   ├── adapters/              # MailSource + AccountingSystem seams, IMAP, folder, fakes
│   ├── engine/                # state machine, planner, executor, verifier
│   ├── context/               # Company Context loader, Work Order
│   ├── india/                 # India GST schema, GSTIN and tax validations
│   ├── llm/                   # provider client, replay, cost meter
│   ├── tools/                 # browser, files, mail, erp, invoice extraction
│   ├── runs/                  # run store, journal, evidence pack
│   └── web/                   # dashboard + API
├── mocks/                     # Mock Suite
│   ├── maildesk/
│   ├── ledgerlite/
│   └── seed/
├── scripts/                   # dev helpers (seed, serve, fixture and demo recording)
├── tests/                     # integration + unit tests
└── docs/
    ├── ARCHITECTURE.md        # this file
    ├── adr/                   # decision records
    ├── specs/                 # archived specs
    └── agents/                # agent skill configuration
```
