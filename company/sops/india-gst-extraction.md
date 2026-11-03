# India GST invoice extraction

Procedure for turning supplier invoice emails into a created purchase bill in
Zoho Books, with citations, validations, matching, and any required human
approval.

## 1. Collect the invoices

- Read the AP mailbox and open every unread invoice email.
- Treat the invoice PDF as the source of truth for the supplier, GSTIN, invoice
  number and dates, amounts, HSN/SAC, and purchase-order reference.
- The mail source stores attachments content-addressed; use the path it returns.

## 2. Extract the India GST fields

- Extract supplier name, GSTIN, invoice number, invoice date, taxable amount,
  CGST, SGST, IGST, total, HSN/SAC, PO reference, due date, and currency.
- Every field must carry a confidence and the exact line that supports it.
- Never guess a value. A missing required field goes to a human. A field below
  the confidence threshold is still proposed: the purchase bill's Approval Gate
  is the human review, and the threshold breach is recorded in the evidence.

## 3. Validate

- The GSTIN format and check digit must be valid.
- Taxable amount plus CGST, SGST, and IGST must equal the invoice total.
- CGST and SGST must be present together and equal; an IGST invoice cannot also
  carry CGST or SGST.
- Record every validation result in the Evidence Pack. The agent does not
  correct a failed validation.

## 4. Match the supplier and purchase order

- Find the supplier by GSTIN and name in the accounting system.
- When the invoice cites a purchase order, read it and compare its amount with
  the invoice total.

## 5. Create the purchase bill after approval

- Propose one purchase bill for the extracted invoice: vendor, invoice number,
  total, currency, and purchase order when one matched.
- Call the bill tool once. Policy decides whether the bill is created now or
  held for a human. Never create a second bill for the same supplier and
  invoice number, and never retry a write whose outcome is unknown.
- A human approval releases the exact prepared bill. A denial stops the Run.

## 6. Archive and report

- Move the source PDF to the processed folder and record where it went.
- A Run is complete only when the source is archived, the purchase bill exists
  exactly once in Zoho Books, and the extraction is recorded with its citations
  and validations. Otherwise escalate to the AP owner.
