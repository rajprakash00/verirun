# ADR 0009: Zoho Books reads and the read-only Verifier token

Status: accepted
Date: 2026-11-02
Task: #43 — Phase 1: Zoho Books read adapter and read-only verification

## Context

ADR 0007 put `AccountingReads` between the Verifier and the system of record,
and left Zoho Books as the first product implementation. The product spec makes
two promises this ticket has to keep:

1. The Verifier reads the system of record through a read-only token, never the
   executor's write client.
2. An invoice is matched to the vendor master, duplicate bills, and purchase
   orders before a purchase bill exists.

Zoho Books India has no goods-receipt entity in Books; goods receipts belong to
Zoho Inventory. Zoho's list endpoints paginate with `page_context`, rate-limit
per organisation, return 5xx under load, and can lag a successful write. The
adapter has to make those behaviours explicit instead of leaking them into the
engine.

## Decision

1. **Two objects, one client seam.** `ZohoAccountingReads` implements
   `AccountingReads` over a `ZohoBooksClient`. `ZohoAccountingSystem` inherits
   the reads and adds the executor's write half. Until the write-path ticket,
   `create_purchase_bill` raises `UnsupportedError`; it never silently changes
   local state. `Backends.reads` is a separate `ZohoAccountingReads` instance,
   so the Verifier's type gives it no write method at all.
2. **OAuth with an encrypted token file.** The operator supplies a client id,
   client secret, refresh token, and token-encryption key. The adapter refreshes
   the short-lived access token, keeps a rotated refresh token, and writes both
   to one Fernet-encrypted file with mode `0600`. A missing or wrong key is an
   actionable conflict; no token appears in a message, log, or Evidence Pack.
   The read-only scope set is `ZohoBooks.contacts.READ`,
   `ZohoBooks.purchaseorders.READ`, `ZohoBooks.bills.READ`, and
   `ZohoBooks.vendorpayments.READ`. A token that carries a `.CREATE`,
   `.UPDATE`, `.DELETE`, or `.ALL` scope is rejected by the read-only client
   instead of being trusted with the label.
3. **One HTTP client owns transport policy.** It walks every page of a list,
   retries 429 and 5xx with exponential backoff, honours `Retry-After`, refreshes
   the access token once on 401, and maps 404 and Zoho code 1002 to
   `NotFoundError`. Other 4xx responses are `ConflictError` with Zoho's message.
4. **Reads normalise to the engine's vocabulary.** Contacts become `Vendor`
   (GSTIN in `tax_id`), purchase orders carry their human-readable number,
   bills map Zoho's `draft`/`open`/`paid`/`void` states to the existing
   `received`/`approved`/`paid`/`rejected`, and money strings become integer
   paise. `find_vendors` and `list_bills` filter locally after reading, so exact
   GSTIN and invoice-number matching does not depend on Zoho's search syntax;
   vendor names compare after case and punctuation are folded away.
5. **Goods receipts are absent, not invented.** `get_goods_receipt` raises
   `NotFoundError` with a message naming Zoho Books. The Mock Suite still
   exercises the goods-receipt path; Zoho bills never carry a `gr_id`.
6. **Eventual consistency is a read-side wait.** `wait_for_bill` polls by vendor
   and number until Zoho returns exactly one bill or the timeout expires. A
   timeout is a `NotFoundError` for the writer to reconcile, never a reason to
   repeat the write.
7. **The Verifier gets a new check, not a second executor.**
   `accounting_invoice_matches` reads the Run's successful `invoice.extract`
   observation for the supplier, GSTIN, number, total, and PO reference, then
   resolves them through `AccountingReads`. It fails on an ambiguous vendor, a
   GSTIN or name mismatch, a live bill with the same number, or a PO that is not
   open or whose amount/currency differs. Evidence references are real system
   ids: `accounting:vendor:<id>`, `accounting:no-bill:<vendor>:<number>`, and
   `accounting:purchaseorder:<id>`; a duplicate failure cites the existing
   `accounting:bill:<id>`.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **One `ZohoAccountingSystem` object for the Verifier and executor** | The Verifier would hold an object with a create-bill method, even if the token were read-only. The separate object makes the trust boundary structural. |
| **Store the token in the environment only** | A restart would need the operator to re-supply it, and the spec requires encrypted at rest. |
| **Keep Zoho's status strings in the engine** | Every check and policy would learn a second vocabulary. The adapter normalises once. |
| **Search vendors and bills server-side only** | Zoho's search semantics differ by endpoint and are not exact; local filtering is the duplicate guarantee. |
| **Model a fake Zoho goods receipt** | It would make the Evidence Pack claim a system record that does not exist. |
| **Retry a bill read forever** | A timeout must surface as an Exception for reconciliation, not hide a lag. |
| **Compare the Verifier's source read to the executor's extraction only** | It would trust the doer. The check reads the extracted facts but resolves every external claim against the system of record; the extraction itself is already checked against citations and GST validations. |

## Consequences

- The product path can match a real invoice against a Zoho Books India trial
  without creating a bill, and the Evidence Pack shows the vendor, duplicate,
  and purchase-order evidence.
- The write ticket can reuse `wait_for_bill` and the token store; it adds a
  write client and a separate write refresh token without changing the Verifier.
- Recorded fixtures cover pagination, rate limits, not-found, eventual
  consistency, and an end-to-end Run; a live contract test runs only when the
  operator exports Zoho credentials.
- Zoho Books is India-only in this adapter; other regions need their own
  accounts and API domains.
- Goods-receipt matching remains a Mock Suite capability until a Zoho Inventory
  connector exists.
