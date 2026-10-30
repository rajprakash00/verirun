# ADR 0007: Adapter interfaces for mail and accounting

Status: accepted
Date: 2026-10-30
Task: #41 — Phase 1: adapter seam and fakes

## Context

The engine and the Verifier read and wrote the Mock Suite directly: the mail
tools opened MailDesk's SQLite file, the ERP tools opened LedgerLite's, and the
Verifier ran SQL against LedgerLite to check a Run's outcome. The India-first
product path needs IMAP mail and Zoho Books, and the product spec makes the
Verifier read the system of record through a read-only token. Landing those
integrations on the current code would mean either a rewrite of every tool and
check or a second, parallel path that only the real integrations use.

Two seams had to exist first: one for mail, one for accounting.

## Decision

Put two interfaces between the engine and the external systems, and make every
tool and check depend on them.

1. `MailSource` lists messages in a folder, reads one message with its
   attachment metadata, opens one attachment's bytes, and marks a message read.
   It exposes a document root that attachment paths are relative to; the file
   tools are rooted there too, so an IMAP or watched-folder source plugs in
   without a second path convention. The Mock Suite mailbox implements it.
2. `AccountingReads` covers vendor, purchase-order, goods-receipt, bill,
   payment, and pending-approval reads. The Verifier depends only on this
   interface, so a product integration can hand it a read-only token.
   `AccountingSystem` adds the write side the executor needs: create a purchase
   bill, plus optional vendor creation and payment scheduling for the Mock
   Suite. The optional capabilities default to `UnsupportedError`.
3. The Mock Suite adapters live in `src/verirun/adapters/mock.py`. The
   in-memory fake in `src/verirun/adapters/memory.py` enforces the same business
   rules as the mock: duplicate bills and vendors, purchase-order and
   goods-receipt amount mismatches, and full-payment-only scheduling. The
   rejection messages and payloads are shared in
   `src/verirun/adapters/rules.py`; parameterized contract tests run both
   implementations through the same scenarios.
4. `runtime.Backends` is the wiring point. The CLI, dashboard, demo, and eval
   build the Mock Suite backends; the in-memory test builds its own. Tools and
   `run_task`/`resume_run`/`verify_run` take the adapters, not database paths.
   `Backends.reads` is where a Zoho read-only token goes; the Verifier uses it
   in preference to the write adapter.
5. Adapter failures are classified (`not_found`, `invalid`, `transient`) and
   carry the comparison or existing record in `data`, which the tools translate
   into the existing Observation error kinds. The engine's failure ladder is
   unchanged.

The observations keep their previous shape, including the fixture `scenario`
field, so the recorded replay fixtures and the eval stay byte-for-byte
valid.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Wrap the SQLite reads in helper functions only** | The Verifier would still be coupled to LedgerLite's schema, and a Zoho adapter would need a second set of helpers and checks. |
| **One `AccountingSystem` interface for reads and writes** | The Verifier would receive a token capable of creating bills. A separate read interface keeps the trust boundary explicit. |
| **A generic repository with arbitrary queries** | It moves SQL into the caller and lets the engine depend on the mock's schema through a different door. |
| **Land IMAP and Zoho first, refactor later** | The real adapters would arrive behind a second path that tests and the demo do not exercise; the Mock Suite path would stay the only proven one. |
| **Drop the mock `scenario` field from observations** | It would invalidate every recorded replay fixture for behaviour that did not change. The field is metadata, not business logic. |

## Consequences

- IMAP, the watched folder, and Zoho Books become adapter implementations, not
  engine changes. The read-only Verifier token is a separate object.
- The Tool layer is thin: argument parsing, policy facts, and Observation
  shaping. Duplicate, mismatch, and full-payment rules live in the adapter, so
  every implementation enforces them.
- The in-memory fake makes engine and crash tests independent of the Mock Suite
  databases; one end-to-end test runs a real Run on it.
- Observations and replay fixtures keep their shape. The Mock Suite still backs
  the demo, eval, and browser scenarios.
- A product adapter must normalise its status vocabulary to the Mock Suite's
  `received`, `approved`, `paid`, and `rejected` until the India Task Pack
  defines its own bill states.
