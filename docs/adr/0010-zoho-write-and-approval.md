# ADR 0010: Zoho Books write path, Approval Gate, and bill verification

Status: accepted
Date: 2026-11-03
Task: #44 — Phase 1: Zoho Books write path and approval gate

## Context

ADR 0009 gave the Verifier a read-only Zoho Books object and left bill creation
explicitly unsupported. The product spec makes the write irreversible and
requires a human decision for the risky cases: a bill above a configurable INR
threshold, a new or unmatched vendor, a missing purchase order when the Task
Pack requires one, and a low-confidence extraction. A retry, restart, or
timeout must never create a second bill or repeat an unknown write.

Zoho Books has no idempotency header. A create can succeed and still return a
5xx or lose its response. The system also has no goods receipts in Books, and a
bill's total is the only money field the existing `Bill` model reads.

## Decision

1. **Two OAuth clients with separate refresh tokens.** `Backends.from_settings`
   builds a read-only `ZohoAccountingReads` for the Verifier and a
   write-capable `ZohoAccountingSystem` for the executor. Each token lives in
   its own Fernet-encrypted file. A partial configuration is an error, never a
   silent fallback to LedgerLite.
2. **The journal still owns the idempotency key.** The engine computes the key
   from Step, Tool, and arguments, records `intent` before the call, and
   `done` after it. The key is injected into the side-effect Tool call as a
   hidden argument; it never appears in the model's observations or the
   Evidence Pack.
3. **Reconcile before retry.** The Zoho adapter writes `verirun:<key>` into the
   bill's `reference_number`. Before a POST it looks for a bill with the same
   vendor and invoice number: our own key means the earlier write landed; a
   bill without our key is a duplicate. After a network error or 5xx it
   reconciles once; if the bill is still not found, the outcome is unknown and
   the Run escalates for a human. Only a 429 is retried, because Zoho rejected
   it.
4. **Policy facts come from the Tool and the Run.** The bill Tool supplies the
   proposed amount, currency, vendor status, and purchase-order presence. The
   policy gate merges facts the engine already recorded: the extraction total,
   supplier GSTIN and name, purchase-order reference, confidence, and whether
   the pack requires a purchase order. The proposed bill's amount, vendor, and
   purchase order must agree with those facts before the gate can pass, so a
   model that understates an amount or names a different vendor parks for a
   human instead of writing. Policies are scoped to the Task Pack, so the India
   rule does not gate the Mock Suite packs.
5. **Low confidence becomes an Approval Gate, not an escalation.** India text
   extraction records `low_confidence` and still succeeds; the bill's gate is
   the human decision point. The US vision path keeps its immediate escalation
   because no bill is created there.
6. **The bill is irreversible.** `erp.file_invoice` sets `irreversible = True`
   and its policy action is `bill.create`, so a gate can prepare it. An
   inactive vendor is a `forbid` rule and returns a denial, never a prepared
   action.
7. **The Verifier re-reads the source.** `india_bill_matches` parses the source
   tax-invoice PDF itself and checks one bill, vendor name and GSTIN, invoice
   number, amount, currency, GSTIN check digit, tax arithmetic, the purchase
   order, and the source reference. `accounting_invoice_matches` treats the
   bill named by this Run's own completed write as its bill, not a duplicate.
8. **The approver sees the evidence.** The dashboard approval review shows the
   source message and attachment, the extraction with per-field confidence and
   citation, the GST validations, and the exact proposed bill arguments.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Use one write token for the Verifier and executor** | The trust boundary in ADR 0009 would disappear; a verification bug could write. |
| **Retry the POST on 5xx** | The first request may have created the bill. The natural-key lookup and key marker are the only safe recovery. |
| **Adopt any bill with the same vendor and number** | A human- or other-system-created duplicate would be accepted as this Run's work. The key marker distinguishes our write. |
| **Have the model pass the extraction confidence into the bill arguments** | The model could understate or omit it; the Run's observation is the record. |
| **Make every India bill wait for approval** | The Task Pack's threshold rule is the configured control; a matched, low-risk invoice may proceed and is still verified. |
| **Check the bill only through the executor's extraction** | The Verifier would trust the doer. It parses the source PDF and reads Zoho through the read-only token. |
| **Store tax components on `Bill` and require Zoho tax IDs** | Tax IDs are organisation-specific and not in the invoice. The source tax arithmetic is verified independently, and Zoho's bill total is checked against it. |

## Consequences

- A parked bill resumes exactly once after approval and a rejected bill writes
  nothing. The Evidence Pack carries the approval, the journaled write with its
  key, and the verification results.
- A crash after Zoho creates the bill but before the journal is marked done is
  safe when the retry uses the same key; the adapter returns the earlier bill.
- Zoho create requests use one line item and do not carry a GST tax breakdown;
  the source arithmetic is verified and the organisation's own tax setup remains
  the accounting system's concern.
- The India policy threshold is 10,000 INR in `company/policies/india-ap.yaml`;
  changing it is a policy edit, not an engine change.
- `wait_for_bill` remains the read-side eventual-consistency helper. A list
  endpoint that lags the create response does not turn a completed write into a
  retry.
