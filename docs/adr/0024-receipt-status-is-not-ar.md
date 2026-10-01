# ADR 0024 — Receipt status is one module, and it is not AR

Status: Accepted · 2026-10-02 (decisions bA and the write-off precedence: Put, 2026-10-01)

## Context

`/ar?tab=invoices`, the reconcile tab's ledger column and the transfer matcher each decided
"is this invoice paid" with the same hand-typed rule: any active receipt link means paid.
`payments_alloc` already had an amount engine that nets credit notes and receipts per invoice,
so three definitions of paid coexisted. On the 2026-09-30 prod snapshot the link rule called
four part-received invoices paid (฿25,122.31 still open) and three invoices fully cleared by
credit notes unpaid, and the reconcile ledger counted two written-off invoices as owed.

## Decision

1. **`inventory_app/receipt_status.py` owns the status.** `rows()` returns one row per
   receivable invoice (a doc_base, not a return, not HS, billed > 0) with `billed`,
   `collected`, `remainder`, `status`, `written_off` and `last_payment_date`, built on
   `payments_alloc.invoice_settlement()`.
2. **Four statuses.** The engine's paid, fully_credited and overpaid read paid; partial reads
   partial; unpaid reads unpaid. An invoice in `ar_writeoffs` (the whole table, the
   collectability reading) reads ตัดหนี้แล้ว unless it was received in full, when it stays
   paid. `written_off` flags every invoice in the table.
3. **Remainder, not billed (decision bA).** The reconcile ledger column and
   `find_payment_candidates` use each partial or unpaid invoice's remainder after credit notes
   and receipts. The matcher never offers a ตัดหนี้แล้ว invoice.
4. **Readers.** `get_payment_status`, `get_payment_summary`, the ledger side of
   `get_ar_reconciliation` and `find_payment_candidates` read the module. The newest receipt's
   number and date still come from `_ACTIVE_PAYMENT_DISPLAY_CTE`, display only.
   `_ACTIVE_PAID_DOCS_CTE` is deleted.
5. **Untouched on purpose.** `payment_speed` keeps excluding the whole `ar_writeoffs` table
   before windowing, `cash_in_rows` keeps feeding `/cashflow`, and the commission engine keeps
   its own stricter receipt rule for payouts.

## Consequences

- Receipt status is never AR. A remainder on the Sendy ledger is not a figure anyone is chased
  for; chaseable stays the Express snapshot (ADR 0012).
- Express closes some invoices short (a few satang to a few hundred baht). They read
  จ่ายบางส่วน here and their remainder appears in the reconcile ledger column and the matcher.
- The invoices tab now computes the whole ledger in Python per request (one engine pass for the
  list, one for the summary cards).
- `tests/test_receipt_status.py` compares the module with the frozen trunk logic in
  `tests/_fixtures/payment_status_9e466e2.py` on random tie-dense ledgers: they agree on every
  invoice without a partial receipt, credit note or write-off, and every disagreement matches
  a named rule.
