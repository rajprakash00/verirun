# ADR 0008: India GST extraction with citations and independent validations

Status: accepted
Date: 2026-10-31
Task: #42 — Phase 1: IMAP ingestion and India GST extraction

## Context

The India-first product path reads supplier invoices from a real AP mailbox and
must show a finance user why each field is believed: the supplier, GSTIN, invoice
number and dates, taxable amount, CGST, SGST, IGST, total, HSN/SAC, PO
reference, due date, and INR. Two facts make this different from the prototype's
US invoice path:

1. Mail arrives over IMAP (or as PDFs in a watched folder), not from a seeded
   SQLite mailbox. Reconnecting, restarting, or a server-side UIDVALIDITY change
   must not reprocess the same invoice.
2. GST has machine-checkable rules. A wrong GSTIN check digit or tax breakdown
   is a business error, regardless of how confident a model is.

The existing vision path returns a fixed US schema, and the Mock Suite mailbox
is the only `MailSource`. Neither covers this work.

## Decision

Keep the two adapter seams from ADR 0007 and add product implementations and an
India extraction tool behind them.

1. **One local index for real mail.** `ImapSource` and `WatchedFolderSource` ingest
   into a small SQLite index (`MailIndex`) and a content-addressed store under
   the document root: `attachments/<sha-prefix>/<sha>/<filename>`. Message ids
   are derived from the SHA-256 of the source bytes, so an attachment, message,
   or reconnect is identified by content. Every attachment row points at its
   source message.
2. **IMAP tracks UIDVALIDITY and UID.** The cursor is `(uidvalidity, last_uid)`
   per folder. A reconnect fetches only `last_uid + 1:*`. When the server starts
   a new UIDVALIDITY epoch, the adapter rescans from the first UID; content
   hashes keep already ingested messages from being stored twice. The local
   index, not the server's `\Seen` flag, is authoritative, so a crash between
   fetch and mark cannot lose an invoice.
3. **The watched folder is a first-class `MailSource`.** Every PDF directly in
   the directory becomes one message with one content-addressed attachment. The
   same PDF through IMAP or the folder produces the same message id and the same
   extraction.
4. **Extraction returns a value, a confidence, and a citation per field.** The
   model replies under a strict JSON schema. A citation must be an exact line
   from the document text, and it must contain the value it supports; a
   fabricated or unsupported citation fails the extraction. A present field
   below the Task Pack's confidence threshold fails the call with
   `low_confidence`, as the vision path already does.
5. **GST validations are deterministic and independent of the model.** The
   GSTIN format and check digit, the tax components summing to the total, and
   the CGST/SGST versus IGST split are computed from the normalised paise values.
   A failed validation does not fail the extraction: it is recorded on the
   Observation and in the Evidence Pack, where the India Task Pack's verification
   contract can act on it.
6. **The Evidence Pack carries provenance.** It now has a `sources` list (each
   message and its attachments, including the content address), the extraction
   with per-field citations and confidences, and a flat `validations` list with
   the source document. The static HTML shows all three.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Parse invoice text with a regex per supplier** | Indian invoice layouts and labels vary; a regex per format is a growing pile of fragile rules. A model under a strict schema, with its output checked against the document and deterministic GST rules, handles unseen layouts while preserving reviewability. |
| **Trust the model's own GSTIN/tax validation** | A model that can hallucinate a value can also hallucinate a pass. The check digit and arithmetic are cheap, exact, and reproducible. |
| **Store attachments by message UID** | UIDs are meaningless across UIDVALIDITY epochs and mailboxes. Content addressing makes deduplication and reconnect safety a property of the bytes, not of server bookkeeping. |
| **Mark the server `\Seen` on sync** | The engine would then depend on a second IMAP write to know a message is ingested. A crash or a read-only mailbox would lose the invoice. The local index is the durable record. |
| **Add a second vision schema for scans now** | Text-layer PDFs are the common emailed-invoice case, and the ticket's acceptance is a reviewable extraction with evidence. The vision path keeps the US schema until the India Task Pack defines the scan scenarios. |
| **Make failed GST validations fail `invoice.extract`** | The extraction succeeded; the invoice is invalid. Recording the verdict lets the Evidence Pack show it and lets the Task Pack decide whether to escalate, re-plan, or stop. |

## Consequences

- IMAP and the watched folder are swaps behind `MailSource`; the CLI and
  dashboard choose one from settings. The Mock Suite stays the demo, eval, and
  test default.
- The local mail index is another SQLite file (`VERIRUN_MAIL_STATE_DB`). It is
  product state, not Mock Suite state.
- Observations keep their existing shape; the evidence gains new top-level keys
  but no existing tool payload changes, so replay fixtures stay valid.
- Ingestion is idempotent per message and attachment. Run-level duplicate
  detection (the same vendor and invoice number across Runs) belongs to the
  Zoho read path and the India Task Pack, not to mail ingestion.
- India extraction currently requires a text-layer PDF. Scans and regional
  languages are follow-ups; the tool says so and points at `files.extract`.
- The GST verification contract and INR approval thresholds (#46), and the Zoho
  Books read and write paths (#43, #44), are later Phase 1 tickets. This ticket
  ends at a reviewable extraction with evidence.
