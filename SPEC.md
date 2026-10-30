# Verirun — Product Spec (v1, India-first)

**Status:** ready for implementation
**Supersedes:** the prototype-phase product spec, archived at `docs/specs/0001-prototype.md`.

## Problem Statement

Indian SMBs and accounting firms receive supplier invoices by email. Someone opens each
mail, reads the attachment, checks the supplier and GST details, matches the invoice to a
purchase order when one exists, and types the purchase bill into Zoho Books or Tally. The
work is slow, repetitive, and easy to get wrong: duplicate invoices, wrong GSTINs, tax
totals that do not add up, amount mismatches, and missing approvals. When a client asks
what happened to an invoice, the answer is scattered across mail, spreadsheets, and the
accounting system. Existing AP automation tools are usually cloud-only, opaque about what
the model did, and difficult to audit.

## Solution

Verirun is a self-hosted AP agent for Indian SMBs and accounting firms. It connects to an
AP mailbox over IMAP and to Zoho Books over OAuth, reads supplier invoices, extracts GST
fields with citations and confidence, validates GSTIN and tax arithmetic, matches the
invoice against the vendor master and purchase orders, and drafts a purchase bill. A human
approves every irreversible action. Verirun then creates the bill in Zoho Books, verifies
the result against the system of record, and writes an Evidence Pack for every invoice.

Verirun runs in Docker on the customer's own infrastructure. Documents and credentials stay
in their environment. It does not move money, file GST, or generate e-invoices. The trust
boundary is explicit: the agent proposes, a human approves, and the Verifier checks the
outcome against Zoho Books rather than against the agent's own messages.

The existing Mock Suite remains the deterministic demo and test fixture. The product path
uses real IMAP mail and a real Zoho Books organisation.

## User Stories

1. As an installer, I want a one-command Docker Compose install, so that I can run Verirun
   on my own server without building a deployment pipeline.
2. As an installer, I want a `verirun doctor` command, so that I can check the database,
   mailbox, Zoho Books connection, disk, and optional browser before processing invoices.
3. As a finance user, I want an onboarding wizard that connects my AP mailbox, so that I do
   not have to edit environment files or understand IMAP settings.
4. As a finance user, I want to connect Zoho Books with OAuth and test the connection, so
   that I know Verirun can read vendors and purchase orders and create bills.
5. As a finance user, I want to choose the mailbox folder or label that contains AP mail, so
   that only relevant messages are processed.
6. As a finance user, I want Verirun to ingest new invoice emails and attachments
   idempotently, so that reconnecting or restarting does not reprocess the same invoice.
7. As a finance user, I want a watched-folder option, so that I can process PDFs without
   giving Verirun mailbox access.
8. As a finance user, I want Verirun to extract supplier name, GSTIN, invoice number,
   invoice date, taxable amount, CGST, SGST, IGST, total, HSN/SAC, PO reference, and due
   date, so that I can review the bill before it is created.
9. As a finance user, I want every extracted field to show a confidence score and a source
   citation, so that I can trust or correct it.
10. As a finance user, I want GSTIN checksum validation, so that invalid supplier GSTINs are
    caught before a bill is created.
11. As a finance user, I want tax-breakdown validation, so that line taxes that do not sum to
    the invoice total are caught.
12. As a finance user, I want vendor matching by GSTIN and name against Zoho Books, so that
    bills are filed against the correct vendor.
13. As a finance user, I want duplicate detection by vendor and invoice number, so that the
    same invoice is not entered twice.
14. As a finance user, I want purchase-order and goods-receipt matching where a PO exists, so
    that amount and quantity mismatches are flagged.
15. As a finance user, I want unmatched or low-confidence invoices to go to an Exception
    queue, so that I can resolve them instead of the agent guessing.
16. As an approver, I want a purchase bill above a configurable INR threshold to wait for my
    approval, so that no irreversible action happens without a human decision.
17. As an approver, I want to see the source invoice, extracted fields, validation results,
    and proposed bill before approving, so that I can make an informed decision.
18. As an approver, I want to approve or reject with a reason, so that the decision is
    recorded in the Run and the Evidence Pack.
19. As a finance user, I want Verirun to create the purchase bill in Zoho Books only after
    approval, so that the system of record is updated deliberately.
20. As a finance user, I want bill creation to be idempotent and crash-safe, so that a retry
    or restart never creates a duplicate bill.
21. As a finance user, I want an unknown outcome, such as a timeout after submission, to park
    for reconciliation, so that Verirun never blindly retries a write.
22. As a finance user, I want Verirun to verify the created bill against Zoho Books, so that I
    know the system of record matches the source invoice.
23. As an auditor, I want an Evidence Pack per invoice with the source document, extraction,
    validations, approvals, verification, and timestamps, so that I can audit what happened.
24. As an auditor, I want the Evidence Pack to be hash-chained or signed, so that I can detect
    tampering after the fact.
25. As an admin, I want local users with admin, approver, and viewer roles, so that only
    authorised people can approve bills.
26. As an admin, I want optional OIDC login, so that I can integrate Verirun with my company
    identity provider.
27. As an admin, I want CSRF protection and secure session cookies, so that the dashboard is
    safe on a company network.
28. As an admin, I want mailbox and ERP credentials encrypted at rest and never written to
    logs, so that access to the server does not expose them.
29. As an admin, I want a retention and PII policy for documents and Evidence Packs, so that
    data is deleted according to policy.
30. As an operator, I want structured logs, health checks, and a runbook, so that I can
    diagnose failures without reading the source.
31. As an operator, I want backup, restore, and upgrade procedures, so that I can run Verirun
    safely over time.
32. As an operator, I want `verirun eval` to run India AP scenarios in replay and live modes,
    so that I can judge a model or prompt change before deploying it.
33. As an operator, I want per-run step and cost budgets and per-company limits, so that a
    runaway Run cannot spend without bound.
34. As a developer, I want the Mock Suite to remain a deterministic test and demo fixture, so
    that CI does not depend on external APIs.
35. As a developer, I want adapter contract tests against a Zoho Books trial and an IMAP test
    mailbox, so that integration drift is caught.
36. As a finance user, I want clear error states and retry guidance, so that I know what to do
    when the mailbox or Zoho Books is unavailable.
37. As a finance user, I want a Run timeline and Evidence Pack viewer, so that I can see what
    Verirun did step by step.
38. As a finance user, I want Verirun to never move money or file GST, so that the risk
    boundary is unambiguous.
39. As a finance user, I want INR amounts in paise and GST tax lines, so that Indian invoices
    are handled without rounding surprises.
40. As a finance user, I want English invoice extraction first, with a clear path for regional
    languages later.

## Implementation Decisions

**Product and deployment**

- Verirun v1 is self-hosted, single-company, and MIT licensed. One install serves one
  company. An accounting firm runs one instance per client; multi-client mode is later.
- The target user is a technical SMB finance team or an accounting firm with IT support. The
  product is installed with Docker Compose and administered through a web UI plus a CLI.
- The product does not move money, execute bank transfers, file GST, or generate e-invoices.
  It creates purchase bills in Zoho Books after human approval.
- The existing Mock Suite remains the offline demo and deterministic test fixture. The
  product path uses real IMAP and Zoho Books.

**Domain and Task Pack**

- A new Task Pack, `invoice-processing-in`, defines the India GST AP workflow: goal,
  allowed tools, approval rules, and verification contract.
- The extraction schema covers supplier legal name, GSTIN, invoice number, invoice date,
  taxable amount, CGST, SGST, IGST, total, HSN/SAC, PO reference, due date, and currency
  (INR).
- Verification checks include GSTIN checksum validity, tax components summing to the total,
  vendor GSTIN matching the Zoho Books vendor, duplicate invoice detection by vendor and
  invoice number, PO/goods-receipt matching where a PO exists, amount tolerance, and the
  extraction confidence threshold.
- Approval rules require a human before creating a bill above a configurable INR threshold,
  for a new or unmatched vendor, for a missing PO when the Task Pack requires one, and for
  low-confidence extraction. A forbidden action remains a denial, not an Approval Gate.
- The existing US-style invoice pack stays for the Mock Suite and demo. It is not the
  product path.

**Integrations**

- Two adapter interfaces separate the engine from external systems: `MailSource` and
  `AccountingSystem`.
- `MailSource` implementations: IMAP (SSL, app password, UIDVALIDITY/UID tracking,
  attachment extraction), watched folder, and the existing Mock Suite mailbox.
- `AccountingSystem` implementations: Zoho Books India, the existing Mock Suite ERP, and an
  in-memory fake for deterministic engine tests.
- Zoho Books uses OAuth2 with separate write and read-only tokens. The executor uses the
  write token; the Verifier uses the read-only token. Tokens are refreshed, encrypted at
  rest, and never logged.
- The Zoho adapter handles pagination, rate limits and backoff, vendor and purchase-order
  reads, purchase-bill creation, and eventual consistency polling.
- IMAP ingestion is idempotent: the same mailbox UID and attachment produce the same source
  document and Run. Gmail and Outlook OAuth are later; v1 uses IMAP app passwords and a
  watched folder.
- Every side effect carries an idempotency key derived from the Run, Step, tool, and
  arguments. The accounting adapter passes the key to Zoho where the API supports it, and
  otherwise reconciles by querying for the natural key before retrying.
- An unknown write outcome parks the Run in an Exception for human reconciliation. It never
  auto-retries a bill creation.

**Durability and state**

- The product run store is PostgreSQL: Runs, Steps, Observations, approvals, escalations,
  journal, checkpoints, verifications, and the Evidence Pack index. SQLite remains the
  zero-dependency demo and test store.
- A worker process claims queued Runs with leases, retries transient failures, and moves
  exhausted work to a dead-letter state. The dashboard and CLI enqueue work; they do not run
  long tasks inside an HTTP request.
- The journal has explicit `intent`, `done`, `failed`, and `unknown` states. `unknown`
  requires reconciliation and cannot be retried automatically.
- The write path uses an outbox: the intent is committed before the external call, the result
  is committed after, and a crash between the two is reconciled on resume.
- A database uniqueness constraint prevents more than one purchase bill for the same vendor
  and invoice number.

**Security and audit**

- Local bootstrap admin; admin, approver, and viewer roles; optional OIDC.
- CSRF protection on every mutating dashboard request; secure, HTTP-only session cookies;
  session expiry.
- Credentials are encrypted at rest with a key supplied by the operator or a KMS. Secrets are
  redacted from logs and Evidence Packs.
- The Evidence Pack is JSON plus a static HTML report, hash-chained and signed so tampering
  is detectable.
- Retention and PII policy covers uploaded documents, extracted text, screenshots, and
  Evidence Packs. Deletion is explicit and audited.
- Email and attachment content is untrusted data, never instructions. Observations carry
  provenance, payment-critical facts require a second source such as the vendor master or a
  PO, and egress is allowlisted. A prompt-injection test corpus is part of the test suite.
- Browser automation is not part of the Zoho Books product path. It remains available for the
  Mock Suite and for a later Tally UI connector.

**Product surface**

- Onboarding: connect mailbox, connect Zoho Books, test both connections, choose the AP
  folder or watched folder, set the approval threshold, and process one test invoice.
- Run list, Run detail with timeline, Exception queue, approval queue, and Evidence Pack
  viewer.
- Settings: mailbox, Zoho connection, approval rules, retention, users, and roles.
- `verirun doctor` checks database, IMAP, Zoho Books, disk, and the optional browser, and
  prints actionable errors.
- `verirun demo` stays offline with the Mock Suite. `verirun eval` supports the India Task
  Pack in replay and live modes and writes a report.

**Packaging and operations**

- Docker Compose services: app, worker, PostgreSQL, and an optional browser. Persistent
  volumes for documents and Evidence Packs. Health checks and restart policies.
- A first-run wizard generates configuration and secrets. Migrations run explicitly or at
  startup with a documented upgrade path.
- Backup and restore scripts, a documented retention policy, and an upgrade guide.
- Structured JSON logs, a metrics endpoint, health and readiness endpoints, and a runbook.
- Versioned releases, a changelog, and a security policy.

**Architecture**

- The custom engine remains (ADR 0002). Replay fixtures (ADR 0004), eval ablations
  (ADR 0006), and browser re-grounding (ADR 0005) remain for the Mock Suite and the browser
  path.
- The Tool layer is refactored so the engine depends on the adapter interfaces rather than
  direct SQLite access. The Mock Suite adapters implement the same interfaces as the real
  ones.
- Two new ADRs are proposed: the Verifier reads the system of record through a read-only
  adapter, and self-hosted integrations use bring-your-own OAuth applications.
- Glossary additions: GSTIN, Purchase Bill, Exception, Zoho Books, IMAP Source.

## Testing Decisions

- Good tests assert external behaviour: Run state, Zoho Books state, Evidence Pack contents,
  HTTP responses, and the eval report. They do not assert private functions or transcript
  text.
- The highest seam is the product end to end: install, onboard, ingest a real invoice from an
  IMAP test mailbox, match it against a Zoho Books trial, approve it, create the purchase
  bill, verify it, and open the Evidence Pack.
- A second, deterministic seam is the existing CLI and dashboard end-to-end path against the
  Mock Suite with a scripted or replay LLM. Existing acceptance and dashboard tests stay.
- Adapter contract tests run against a Zoho Books trial and an IMAP test mailbox. They cover
  OAuth refresh, pagination, rate limits, vendor and PO reads, bill creation, duplicate
  responses, and eventual consistency. CI uses recorded fixtures; the live contract job is
  opt-in.
- An in-memory fake adapter supports engine and crash tests without external systems.
- Crash-safety tests kill the worker at each transition, resume, and assert that no duplicate
  purchase bill exists. An unknown outcome must produce an Exception, not a retry.
- Security tests cover authentication, RBAC, CSRF, path traversal, secret redaction, and
  prompt injection through invoice text and attachments.
- Browser tests cover the onboarding wizard, approval queue, and Evidence Pack viewer with
  real Chromium against the local app.
- Eval tests cover the India scenarios: happy path, duplicate invoice, GSTIN mismatch, tax
  mismatch, missing PO, over-limit approval, low confidence, and transient Zoho failure. The
  report asserts terminal state, verification results, human interventions, steps, cost, and
  latency.
- Prior art: the existing CLI, acceptance, dashboard, tool, eval, and browser-regrounding
  tests.

## Out of Scope

- A Tally connector or Tally export. Tally is the largest Indian SMB accounting system, but
  its desktop/XML gateway is a later integration.
- Payments, bank transfers, UPI, NEFT, RTGS, and any movement of money.
- GST filing, e-invoice IRN generation, GSP integration, and e-invoice QR/IRN validation.
  GSTIN checksum validation is in scope; full e-invoice compliance is not.
- Multi-tenant SaaS, signup, billing, and multi-client support in one install.
- QuickBooks Online, Xero, NetSuite, SAP, and Zoho Books outside India.
- Gmail and Outlook OAuth in v1. IMAP app passwords and the watched folder come first.
- Vendor onboarding, purchase-order creation, and reconciliation beyond AP purchase bills.
- Long-term learning or memory, a Task Pack compiler, and a public benchmark.
- Mobile apps, regional-language extraction, and a native desktop app.
- A hosted managed service. A single-tenant managed deployment can be considered later.

## Further Notes

- **Assumptions:** India-first; technical ICP; self-hosted bring-your-own OAuth; no external
  design partner yet; the first pilot is a scripted end-to-end run with a fellow developer
  acting as the finance user; no payments.
- **Phase plan:** this is a high-level roadmap, not a ticket list.
  - **Phase 0:** product spec, pilot script, Zoho Books trial and IMAP test setup.
  - **Phase 1:** adapter interfaces, IMAP ingestion, India GST Task Pack, Zoho Books read
    and write, approval, durable execution, crash safety.
  - **Phase 2:** authentication and RBAC, secrets and encryption, packaging, onboarding,
    Exception queue, Evidence Pack viewer, operations runbook.
  - **Phase 3:** scripted pilot, live-model eval matrix, hardening, documentation, and a
    case study.
- **Success metrics:** a stranger can install Verirun and process 20 GST invoices end to end;
  zero duplicate bills under crash tests; every completed bill has an Evidence Pack; p50 and
  p95 processing latency; cost per invoice; Exception rate; approval rate.
- **Risks:** Tally dominance and the temptation to integrate it too early; Zoho OAuth setup
  friction for self-hosted users; GST and e-invoice complexity; Zoho API rate limits and
  eventual consistency; price sensitivity of Indian SMBs; the support burden of self-hosted
  software.
- **Claims:** the product may be described as production-hardened and self-hosted. It may not
  be described as validated by external customers until a real pilot has run.
