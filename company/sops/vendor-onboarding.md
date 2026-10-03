# Vendor onboarding

Procedure for creating a new vendor in LedgerLite from an onboarding email in
the procurement mailbox.

## 1. Collect the request

- Read the procurement mailbox (`procurement@company.example`) and open the
  vendor onboarding email.
- Save the tax form attachment to the Run's working folder in the shared file
  tree (`working/<run-id>/`).

## 2. Check for duplicates

- Search LedgerLite for the company name and tax id. If a matching vendor
  exists, do not create a second one; report the existing vendor instead.

## 3. Enforce the required documents

- A W-9 (or equivalent tax form) is required. Extract the legal name, tax id,
  address, and signer from the form. Never invent a tax id.
- If the tax form is missing or unreadable, record an open question.

## 4. Create the vendor

- Create the vendor in LedgerLite with the extracted legal name, tax id,
  address, and contact email. Vendor creation is an irreversible action and
  waits for a human approval gate.

## 5. Archive, verify, and report

- Move the tax form to the archive folder and record where it went.
- Verify the vendor exists in LedgerLite exactly once with the expected tax id
  and contact fields, then report.
