# ADR 0011: Durable execution — PostgreSQL, leased workers, and an outbox

Status: accepted
Date: 2026-11-03
Task: #45 — Phase 1: durable execution and crash safety

## Context

The engine already journaled a side effect before and after it ran, but the
journal only had `intent`, `done`, and `failed`. A crash after an external call
but before `done` left an `intent` that a resume re-ran, and a timeout whose
outcome was genuinely unknown was classified as a failure and could be retried.
The CLI and dashboard also executed the whole Run inside the calling process, so
a model or ERP stall held the HTTP request open.

The product spec requires PostgreSQL for the run store and worker queue, leases,
an outbox with explicit `unknown`, reconcile-before-retry, an Exception for a
human, and a uniqueness constraint for vendor invoice numbers. SQLite must stay
the zero-dependency demo and test store.

## Decision

1. **One store, two databases.** `RunStore` talks to a small database adapter
   (`verirun.runs.db`). SQLite keeps the zero-dependency demo/test path;
   PostgreSQL is selected by `VERIRUN_DATABASE_URL`. Both run the same schema
   and the same store methods, so the engine and tests exercise one behaviour.
2. **The journal is the outbox.** `record_action` writes the `intent` and its
   natural-key claim in one transaction *before* the external call. `done` is
   written after success, `failed` after a definite failure, and `unknown` when
   the effect may have landed but the system cannot say. `unknown` is never
   invoked again and parks the Run on an Exception.
3. **Reconcile before retry.** On resume, an `intent` entry asks the Tool
   whether its write already landed. `erp.file_invoice` searches the accounting
   system for a bill carrying this Run's idempotency marker in its reference
   number. A match is adopted without a second write; no match is safe to
   invoke; a read that cannot answer is treated as unknown. The Zoho adapter
   already reconciles internally after a network error or 5xx.
4. **A natural-key claim is a uniqueness constraint in the run store.**
   `side_effect_claims` has `claim_key` as its primary key, and a purchase bill's
   key is `bill:<vendor_id>:<number>`. A second Run cannot claim the same vendor
   invoice while the first is unresolved; it escalates as a duplicate instead of
   writing. LedgerLite and the in-memory fake keep their own unique constraint
   and idempotent create as the system-of-record backstop.
5. **The worker leases jobs.** `work_queue` has one active row per Run and kind.
   SQLite claims under `BEGIN IMMEDIATE`; PostgreSQL claims with `FOR UPDATE SKIP
   LOCKED`. A worker renews its lease while running; a killed worker leaves the
   lease behind, and another worker claims the Run after expiry. Exhausted jobs
   move to a `dead` state with the last error.
6. **Producers enqueue; consumers execute.** `verirun run` creates the Run and
   queues a `start` job unless `--wait` is passed for local convenience. The
   dashboard approval POST records the decision and queues a `resume` job.
   `verirun worker` is the consumer. This keeps HTTP requests and CLI startup
   short and makes restarts ordinary.
7. **Exceptions are first-class records.** `run_exceptions` stores the journal
   key, action, arguments, claim key and detail, adapter error, and a question
   for the human. The Run stays in `needs_human`; the dashboard and the Evidence
   Pack show the Exception with the evidence needed to reconcile it.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Run everything in the dashboard request** | A slow model or ERP holds the HTTP worker and a restart loses the work. |
| **Retry an `intent` blindly** | The first call may have created the bill; the only safe path is reconcile first. |
| **Treat an unknown outcome as `failed`** | It invites a duplicate write. Unknown is a distinct state with a human decision. |
| **Use an external queue (Celery, SQS)** | The product is self-hosted and single-company; the run store is already the durable state, so a leased table avoids another service. |
| **Keep only SQLite** | SQLite serialises writers and cannot provide `SKIP LOCKED` across worker processes. |
| **Let Zoho enforce the duplicate rule** | Zoho Books has no unique constraint on vendor invoice numbers and no idempotency header. |

## Consequences

- A crash before `intent`, after `intent`, after the call, or after `done`
  resumes to exactly one bill. `tests/test_crash_safety.py` asserts all four.
- A worker failure requeues with backoff and eventually dead-letters; a killed
  process leaves a lease that expires instead of a job stuck forever.
- PostgreSQL is required for multi-process production use. SQLite remains the
  demo and test default, and `verirun demo`/`verirun eval` never need a server.
- The natural-key claim survives a crash. A different Run for the same invoice
  parks as a duplicate until a human resolves the first claim, which is the
  desired safety trade-off.
- A human can mark an Exception resolved with a note. Choosing `landed` records
  the external result and marks the outbox entry done; choosing `not_landed`
  marks it failed and releases the natural-key claim, so a resume may retry.
- The journal and natural-key claim are the write fence even if an expired lease
  lets a stale worker finish late: it cannot write a second bill or complete a
  job another worker now owns.
