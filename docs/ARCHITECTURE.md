# Architecture (High-Level Design)

Operator turns a short Request into completed work: it plans, executes with real tools,
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
                        │  Resolve → Plan → Approve →   │
                        │  Execute → Observe → Verify → │
                        │  Report                       │
                        └───┬──────────────────┬────────┘
                            │                  │
             ┌──────────────▼─────┐   ┌────────▼──────────┐
             │    Tool Layer      │   │     Verifier      │
             │ browser / files /  │   │ reads ground      │
             │ mail / erp         │   │ truth, not the UI │
             └──────────┬─────────┘   └────────┬──────────┘
                        │                      │
             ┌──────────▼──────────────────────▼──────────┐
             │                 Mock Suite                  │
             │  MailDesk (webmail)   LedgerLite (ERP)      │
             │  shared/ file tree    SQLite state          │
             └─────────────────────────────────────────────┘

 Company Context (SOPs, policies, registry) ──► Orchestrator
 Task Packs (YAML)                          ──► Orchestrator
 LLM client (live | replay)                 ──► Orchestrator + Verifier
```

## 2. Engine

- `Resolve`: turn the Request + Company Context into a Work Order (assumptions, steps,
  systems, approval gates, success criteria). Ambiguity is resolved from policy or escalated.
- `Plan`: produce an ordered plan of Steps from the Work Order and the Task Pack.
- `Approve`: pause at Approval Gates before irreversible actions.
- `Execute`: a ReAct-style tool loop. One Step at a time: think, call one Tool, observe.
- `Observe`: record the Observation; update run state.
- `Verify`: hand over to the Verifier.
- `Report`: produce the Evidence Pack.

Failure escalation order is fixed: **retry → alternate strategy → re-plan → escalate**.
A single orchestrator runs the loop; the Verifier is a separate phase, not a second worker.

## 3. Company Context and the Work Order

`company/` is committed knowledge: SOPs (Markdown), policies (YAML: spend limits,
permitted/forbidden actions), a system registry (URLs, sandbox credentials), and
precedents. The Work Order is the Operator's written interpretation of a Request and is
shown in the dashboard before execution.

## 4. Task Packs

One declarative YAML file per kind of work: goal template, SOP reference, tool allowlist,
policy references, approval rules, and a verification contract. The engine never contains
task-specific logic; swapping the Task Pack swaps the task. Demonstrated with
`invoice-processing` and `vendor-onboarding`.

## 5. Mock Suite

- **MailDesk**: webmail UI with seeded invoice and vendor emails plus attachments.
- **LedgerLite**: ERP UI with vendors, purchase orders, goods receipts, invoices, payments,
  and approvals; exposes an API used by the Verifier.
- **shared/**: documents (PDFs, scans, tax forms) and archive folders.
- **SQLite**: system state; seed scripts create all scenarios.

## 6. Human-in-the-loop

Approval Gates are declared per Task Pack. Irreversible actions (schedule payment above
limit, create vendor, send external email) may be prepared but not submitted. Decisions
arrive through the dashboard approval queue. Timeouts never auto-approve. Ambiguity or
repeated failure escalates with a question.

## 7. Verification

The Verifier reads the Task Pack's verification contract and checks real state through the
LedgerLite API/database and the filesystem. It does not read the executor's messages and
does not trust screenshots. Output: pass/fail per criterion with evidence references.
Screenshots are display evidence only.

## 8. Reliability and state

- Run store (SQLite): run state, checkpoints, observations, Evidence Pack index.
- Action journal with idempotency keys: retries never repeat completed side effects.
- Checkpointing: a failed run resumes from its last checkpoint.
- Step and cost meters per run; the run stops at its limit.
- Failures are scenario-driven and seeded, never random.

## 9. LLM layer and cost control

- OpenAI-compatible client with a provider base URL and model roles in configuration.
- Roles: fast tool-loop model, stronger plan/verify model, vision model for scans.
- Replay mode serves recorded fixtures for deterministic, offline, zero-cost tests.
- Stable per-run session ID enables prompt caching.
- Fallback provider configurable; the direct provider key is optional.

## 10. Tech stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.12+ with `uv` |
| Data models | Pydantic |
| Browser | Playwright (accessibility-tree refs) |
| Mock apps | FastAPI + SQLite + Jinja templates |
| Dashboard | FastAPI + lightweight HTML/JS |
| PDFs | pypdf for text, render-to-image + vision model for scans |
| Files | pathlib + openpyxl where spreadsheets appear |
| Tests | pytest, replay LLM fixtures |
| Lint/format | Ruff |
| Evidence | JSON + generated static HTML |

## 11. Folder structure

```
proxy-work/
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
│   └── vendor-onboarding.yaml
├── src/company_operator/
│   ├── engine/                # state machine, planner, executor, verifier
│   ├── context/               # Company Context loader, Work Order
│   ├── llm/                   # provider client, replay, cost meter
│   ├── tools/                 # browser, files, mail, erp
│   ├── runs/                  # run store, journal, evidence pack
│   └── web/                   # dashboard + API
├── mocks/                     # Mock Suite
│   ├── maildesk/
│   ├── ledgerlite/
│   └── seed/
├── scripts/                   # dev helpers (seed, record fixtures, serve)
├── tests/                     # integration + unit tests
└── docs/
    ├── ARCHITECTURE.md        # this file
    ├── adr/                   # decision records
    └── agents/                # agent skill configuration
```
