> **Archived:** this is the delivered prototype-phase spec, kept for reference. The current product spec is [`SPEC.md`](../../SPEC.md).

## Problem Statement

Company tasks arrive as short requests that leave the required steps and context unstated. The work itself is spread across web apps, documents, files, and internal systems. People must find the right information, follow procedures, fill in forms, check results, and prove what happened. AI assistants can answer questions about this work, but they do not complete it. As a result the work is slow, inconsistent, and hard to audit.

## Solution

Verirun is a system that turns a short Request into completed work. For each Request it:

- reads the Company Context (SOPs, Policies, system registry, precedents) to discover the unstated steps;
- writes a Work Order: assumptions, steps, systems, approval gates, and success criteria;
- plans and executes with real tools (browser, files, mail, internal systems);
- observes results and adapts: retry, alternate strategy, re-plan, or escalate;
- stops at Approval Gates before irreversible actions;
- verifies the outcome independently against ground truth, not against its own messages;
- returns an Evidence Pack: result, proof, action log, open questions.

The prototype demonstrates two different tasks on one unchanged engine: invoice processing and vendor onboarding. Both run against a self-contained Mock Suite.

## User Stories

1. As an operations manager, I want to give a one-line request, so that I do not have to spell out every step.
2. As an operations manager, I want Verirun to write a Work Order before acting, so that I can see its assumptions and plan.
3. As an operations manager, I want Verirun to read company SOPs and policies, so that work follows our procedures.
4. As an operations manager, I want Verirun to name the systems and permissions it will use, so that I can judge the scope.
5. As a finance operator, I want invoice emails fetched from the mailbox, so that I do not copy data by hand.
6. As a finance operator, I want invoice data extracted from PDFs, so that entry is automatic.
7. As a finance operator, I want text and scanned invoices both supported, so that paper is not a blocker.
8. As a finance operator, I want extracted values validated against purchase orders and goods receipts, so that mismatches are caught.
9. As a finance operator, I want duplicate invoices detected, so that we never pay twice.
10. As a finance operator, I want invoices above the spend limit routed to a human, so that policy is respected.
11. As a finance operator, I want to approve or reject pending actions in one place, so that I stay in control.
12. As a finance operator, I want Verirun to prepare but never submit irreversible actions before approval.
13. As a finance operator, I want transient failures retried automatically, so that hiccups do not need my attention.
14. As a finance operator, I want Verirun to escalate when it cannot proceed, so that I can help.
15. As a finance operator, I want processed invoices archived with their source files, so that records stay tidy.
16. As a finance operator, I want an Evidence Pack for every run, so that I can audit what happened.
17. As a finance operator, I want verification against the ERP record, so that I know the work is actually done.
18. As a procurement operator, I want new vendors onboarded from an email without manual entry, so that setup does not wait on me.
19. As a procurement operator, I want duplicate vendors detected before creation, so that our master data stays clean.
20. As a procurement operator, I want required tax documents enforced, so that compliance is complete.
21. As a compliance officer, I want a Policy denial to stop an action, so that Verirun cannot exceed its permissions.
22. As a compliance officer, I want every action logged with its policy basis, so that decisions are explainable.
23. As an engineer, I want each kind of work declared as a Task Pack, so that adding a task does not change the engine.
24. As an engineer, I want tools behind a uniform interface, so that new tools do not change the engine.
25. As an engineer, I want an injectable LLM client with a replay mode, so that tests are deterministic and cheap.
26. As an engineer, I want a run journal with idempotency keys, so that retries do not repeat completed side effects.
27. As an engineer, I want per-run step and cost limits, so that a run cannot run away.
28. As an engineer, I want failed runs to be resumable from their last checkpoint, so that progress is not lost.
29. As an operator, I want a dashboard timeline of steps and tool calls, so that I can watch and debug runs.
30. As an operator, I want to answer escalations from the dashboard, so that blocked runs resume.
31. As a reviewer, I want the run to record observations and decisions, so that behavior is explainable.
32. As a reviewer, I want known limitations documented, so that expectations are clear.

## Implementation Decisions

**Engine.** A custom state machine with states Resolve, Plan, Approve, Execute, Observe, Verify, Report. Inside Execute, a ReAct-style tool loop runs one step at a time: think, call one tool, observe, repeat. Failures resolve in a fixed escalation order: retry, alternate strategy, re-plan, escalate. The engine is a single orchestrator; no multi-agent crew. A separate Verifier is invoked as its own phase.

**Verifier.** Reads the verification contract from the Task Pack and checks real state via the internal system API/database and the filesystem. It does not read the executor's messages and does not trust the screen. It returns pass/fail per criterion plus evidence references. Screenshots are supporting display evidence only.

**Company Context.** A committed pack: SOPs in Markdown, policies in YAML (spend limits, permitted and forbidden actions), a system registry (URLs and sandbox credentials), and precedents. From a short Request, Verirun produces a Work Order that resolves ambiguity from this context or escalates the gap.

**Task Packs.** Declarative YAML per kind of work: goal template, SOP reference, tool allowlist, policy references, approval gate rules, and a verification contract. The engine is task-agnostic; changing the Task Pack changes the task.

**Tools.** A uniform tool interface. Browser tools use Playwright with accessibility-tree snapshots and deterministic element refs; file tools read, write, move, and archive documents; mail tools read the mailbox; ERP tools are used for verification and permitted direct reads. The browser is real; it is not mocked.

**Mock Suite.** Two small FastAPI services plus local state: MailDesk (webmail with invoice emails and attachments) and LedgerLite (vendors, purchase orders, goods receipts, invoices, payments, approvals). SQLite holds state; a shared file tree holds documents. Seed data scripts create the scenarios.

**Human-in-the-loop.** Approval Gates are declared per Task Pack and enforced by the engine. Verirun may prepare an irreversible action, but submission waits for a human decision in the dashboard approval queue. Timeouts never auto-approve; they abort or remain pending. Ambiguity escalates.

**State and reliability.** A run store (SQLite) keeps run state, checkpoints, an action journal with idempotency keys, and observations. Retries are safely repeatable. Per-run step and cost meters stop runaway runs. Failure handling is scenario-driven, not random.

**LLM layer.** A provider-agnostic OpenAI-compatible client. Live mode points at an OpenAI-compatible gateway; replay mode serves recorded fixtures for deterministic tests. Model roles: a fast tool-loop model, a stronger plan/verify model, and a vision model for scanned documents. Prompt caching is enabled via a stable per-run session ID. An alternative provider is configurable through environment settings.

**Scenarios.** Happy path; duplicate invoice; amount mismatch; missing purchase order; over-limit approval; forbidden action attempt; transient UI failure with retry; image-only scanned invoice with confidence threshold.

**Interfaces.** A CLI runner to start a run from a Request, a local web dashboard showing the Work Order, plan, live timeline, approval queue, and evidence, and a static HTML Evidence Pack generated per run.

**Architecture documentation.** A high-level design document covering components, tech stack, data flow, and folder structure ships with the repo.

## Testing Decisions

Testing is deliberately lean. The goal is to lock critical outcomes, not to maximize coverage.

- **Replay brain.** LLM calls are recorded once and replayed in tests, so tests are deterministic, free, and offline.
- **End-to-end outcome tests** drive the whole Verirun in-process against the real Mock Suite and assert on: the verified result, the Mock Suite ground truth, the Evidence Pack, and the final run state. Scenarios covered this way: happy path, duplicate invoice, amount mismatch, over-limit approval, transient-failure retry.
- **Human-gate tests** drive the dashboard HTTP API: a run pauses at an Approval Gate, an approve or reject decision resumes it, and timeout does not auto-approve.
- **Two small unit seams**: Task Pack validation and Policy evaluation, as table-driven tests.
- **Mock Suite smoke test** only: the mocks are test infrastructure and ground truth, not the product.
- Tests assert external behavior only; no tests on private functions or transcript text.

## Out of Scope

- Native desktop GUI control (documented as a known limitation).
- Real third-party systems, credentials, or payments. All external effects are simulated in the Mock Suite; payment is scheduling, not transfer.
- Authentication, multi-tenancy, and cloud deployment.
- General-purpose task coverage beyond the two demonstrated Task Packs.
- Long-term learning or memory beyond precedent documents.
- Mobile interfaces.

## Further Notes

- A README covers setup, run instructions, architecture, design decisions, assumptions, and known limitations.
- A short demo recording shows a run recovering from a failure, pausing for approval, and verifying the outcome.
- Key technical decisions are recorded as ADRs in `docs/adr/`.
- The design favors a narrow, genuinely autonomous system over broad simulated behavior.
