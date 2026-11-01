# India GST invoice extraction

Procedure for turning supplier invoice emails into a reviewable India GST
extraction with citations and validations. This phase stops before any purchase
bill is created.

## 1. Collect the invoices

- Read the AP mailbox and open every unread invoice email.
- Treat the invoice PDF as the source of truth for the supplier, GSTIN, invoice
  number and dates, amounts, HSN/SAC, and purchase-order reference.
- The mail source stores attachments content-addressed; use the path it returns.

## 2. Extract the India GST fields

- Extract supplier name, GSTIN, invoice number, invoice date, taxable amount,
  CGST, SGST, IGST, total, HSN/SAC, PO reference, due date, and currency.
- Every field must carry a confidence and the exact line that supports it.
- Never guess a value. A missing required field or a field below the confidence
  threshold goes to a human.

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
- Do not create a purchase bill in this phase.

## 5. Archive and report

- Move the source PDF to the processed folder and record where it went.
- A Run is complete only when the source is archived and the extraction is
  recorded with its citations and validations. Otherwise escalate to the AP
  owner.
