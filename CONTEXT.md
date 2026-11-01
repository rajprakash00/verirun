# Context

Shared vocabulary for this project. A glossary only. No implementation details.

**Verirun** — the system that turns a company request into completed work. It understands, plans, executes, observes, adapts, verifies, and reports. We say "Verirun", not "AI employee".

**Request** — a short instruction from a person. A Request can be incomplete.

**Work Order** — Verirun's written interpretation of a Request: assumptions, required steps, systems, policies, approval gates, and success criteria. Made before acting.

**Company Context** — the knowledge needed to complete work: SOPs, policies, system registry, and precedents. Verirun reads it; it does not guess.

**SOP** — Standard Operating Procedure. A written procedure for one kind of work.

**Policy** — a rule that permits or limits an action.

**Approval Gate** — a point where Verirun stops and waits for a human yes/no.

**Approval Request** — a prepared irreversible action waiting for a human yes/no. Approving submits it; rejecting aborts the Run with the reason.

**Escalation** — asking a human for help when Verirun cannot continue safely.

**Tool** — one function Verirun can call.

**Step** — one piece of work inside a plan.

**Run** — one execution of one Work Order, start to end. Has an ID and a state.

**Observation** — the result of one Tool call.

**Re-grounding** — recovering a browser control whose reference has gone stale: Verirun takes a fresh snapshot, finds the control again by role and accessible name, and falls back to the vision model when the accessibility tree lacks it. The locator it used is recorded so a retry touches the same control.

**Verifier** — the part that checks the result of a Run against real system state. Separate from the doer. Does not trust messages or screens.

**Ablation** — an eval-only passing of the same scenarios with one control disabled, to measure what that control catches.

**Silent-failure probe** — a seeded scenario whose doer reports success that the real system state does not support, so only the Verifier can catch it.

**Evidence Pack** — the output of a Run: result, proof, action log, open questions.

**Task Pack** — the declarative definition of one kind of work: goal template, SOP, tool allowlist, verification contract. Changing the Task Pack changes the task; the engine stays the same.

**LedgerLite** — the mock ERP internal system (vendors, POs, goods receipts, invoices, payments).

**MailDesk** — the mock webmail system where requests and invoices arrive.

**Mock Suite** — MailDesk + LedgerLite + the shared file tree + SQLite state.

**Mail Source** — a system that supplies messages, their bodies, and their attachments. MailDesk is a Mail Source; so are an IMAP mailbox and a watched folder.

**IMAP Source** — a Mail Source that reads an AP mailbox over IMAP with an app password and remembers how far it has read, so a reconnect does not reprocess a message.

**Watched Folder Source** — a Mail Source that turns invoice PDFs dropped into a local directory into messages, without mailbox access.

**Accounting System** — the system of record for vendors, purchase orders, and purchase bills. The Verifier reads it; the executor writes purchase bills to it. LedgerLite is an Accounting System; Zoho Books will be.

**Purchase Bill** — the bill Verirun creates in the accounting system for a supplier invoice. LedgerLite calls it an invoice.

**GSTIN** — the 15-character Goods and Services Tax identification number of a registered Indian supplier. Its last character is a check digit, so a mistyped GSTIN can be caught before a bill is created.

**Citation** — the exact line of a source document that supports one extracted field. A value without a citation is not reviewable.

**GST Validation** — a deterministic check of an extracted India invoice: the GSTIN format and check digit, the tax components summing to the total, and a valid CGST/SGST or IGST split. It runs on the values, not on the model's report.
