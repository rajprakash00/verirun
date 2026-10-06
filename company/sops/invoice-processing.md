# Invoice processing

Procedure for turning supplier invoice emails in the AP mailbox into filed,
validated invoices in LedgerLite and scheduled payments.

## 1. Collect the invoices

- Read the AP mailbox (`ap@company.example`) and open every unread invoice email.
- Save each invoice attachment to the Run's working folder in the shared file
  tree (`working/<run-id>/`) before touching the ERP.
- Treat the invoice PDF as the source of truth for invoice number, vendor,
  amount, purchase order, and goods receipt references.

## 2. Extract the invoice fields

- Extract: vendor name, invoice number, amount, purchase order, goods receipt,
  invoice date, and payment terms.
- Support text-layer PDFs and image-only scans. A scan must be read with the
  vision model and its extracted values flagged with a confidence value.
- If a field is missing or unreadable, stop and record an open question. Never
  guess an amount, invoice number, or vendor.

## 3. Validate against the ERP before filing

- Find the vendor in LedgerLite. The vendor must exist and must not be blocked.
- Match the invoice to its purchase order and goods receipt.
- The invoice amount must equal the purchase order amount. Record a mismatch as
  an exception instead of filing it.
- Check for duplicates: an invoice with the same vendor and invoice number that
  already exists or is paid must not be filed again.

## 4. Respect the spend limit

- Apply the spend-limit policy before scheduling payment. An invoice above the
  approval threshold prepares a payment but waits for a human approval gate.
- Never schedule a payment to a blocked vendor. The action-rules policy forbids
  it, and no side effect may happen for a forbidden action.

## 5. File, schedule, and archive

- File the validated invoice in LedgerLite against its purchase order and goods
  receipt.
- Prepare the payment schedule. Submission waits for any required approval.
- Move the source PDF and its scan, if any, to the processed folder and record
  where it went.

## 6. Verify and report

- Verify against LedgerLite and the filesystem: the invoice record exists with
  the right amount and status, and the source document is archived.
- A run is complete only when every success criterion passes. Otherwise report
  the exception and escalate to the AP owner; the Verirun dashboard delivers
  the question to them and resumes the Run with their answer.
