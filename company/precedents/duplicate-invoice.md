# Precedent: duplicate invoice

Date: 2026-08-12
Task: invoice-processing

An invoice from Apex Office Supplies arrived a second time with the invoice
number AQ-2026-014. The prior copy had already been filed and paid. The
Operator stopped before filing, recorded the existing invoice id, and reported
the duplicate to the AP owner instead of filing it again.

Rule of thumb: vendor + invoice number identifies an invoice. If that pair
already exists in LedgerLite, never file or pay a second time.
