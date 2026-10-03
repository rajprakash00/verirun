# Context

Shared vocabulary for this project. A glossary only. No implementation details.

**Operator** — the system that turns a company request into completed work. It understands, plans, executes, observes, adapts, verifies, and reports. We say "Operator", not "AI employee".

**Request** — a short instruction from a person. A Request can be incomplete.

**Work Order** — the Operator's written interpretation of a Request: assumptions, required steps, systems, policies, approval gates, and success criteria. Made before acting.

**Company Context** — the knowledge needed to complete work: SOPs, policies, system registry, and precedents. The Operator reads it; it does not guess.

**SOP** — Standard Operating Procedure. A written procedure for one kind of work.

**Policy** — a rule that permits or limits an action.

**Approval Gate** — a point where the Operator stops and waits for a human yes/no.

**Approval Request** — a prepared irreversible action waiting for a human yes/no. Approving submits it; rejecting aborts the Run with the reason.

**Escalation** — asking a human for help when the Operator cannot continue safely.

**Tool** — one function the Operator can call.

**Step** — one piece of work inside a plan.

**Run** — one execution of one Work Order, start to end. Has an ID and a state.

**Observation** — the result of one Tool call.

**Verifier** — the part that checks the result of a Run against real system state. Separate from the doer. Does not trust messages or screens.

**Evidence Pack** — the output of a Run: result, proof, action log, open questions.

**Task Pack** — the declarative definition of one kind of work: goal template, SOP, tool allowlist, verification contract. Changing the Task Pack changes the task; the engine stays the same.

**LedgerLite** — the mock ERP internal system (vendors, POs, goods receipts, invoices, payments).

**MailDesk** — the mock webmail system where requests and invoices arrive.

**Mock Suite** — MailDesk + LedgerLite + the shared file tree + SQLite state.
