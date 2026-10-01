# ADR 0023 — Chaseable is the only amount owed, and `ar_statement.py` is its one reader

Status: Accepted · 2026-10-02 (grilling rounds and the interrogate verdict: Put, 2026-10-01)

## Context

ADR 0012 named two populations, **outstanding** and **chaseable**, and made every chase-facing page
import `BSN_AR_PREDICATE` instead of re-typing it. The predicate was shared; the queries around it were
not. Five places answered "what does this customer owe", each with its own SELECT:
`models.payments._unpaid_bills` (customer page, `/m/customer`), `ar_followup.get_customer_ar_detail`
(dunning detail, call card), the inline SELECT in `express_ar_customer`, `ar_followup.customer_ranking`
(`/ar` customers tab, `/call` badges) and `models.payments.get_customer_debt_summary` (`/ar` overview,
reconcile). They agreed on the 2026-09-30 prod snapshot (31 customers, 213 documents, ฿507,405.73),
but nothing made them.

Two pages did not agree with the rest:

- **`/call` badges** indexed every balance by name as well as by code and looked the name up first, so
  two customers sharing a name both showed one of their balances. A bare `except Exception` returned an
  empty map on any error, which reads as "nobody owes anything".
- **`/m/sales-trip`** summed the Sendy ledger (`sales_transactions` minus active receipts minus
  `ar_writeoffs`), a third population. On the same snapshot it showed ฿467,190.74 against ฿507,405.73:
  the marketplace exclusion (by design), one code missing from the customer master (01ด03, ฿1,764.00),
  and rounding.

The dunning page also accepted a bare bill name as its key, and its log form could write a follow-up row
with no customer code.

## Decision

1. **`inventory_app/ar_statement.py` owns chaseable AR.** It holds `BSN_AR_PREDICATE` and
   `AR_SNAPSHOT_STALE_AFTER_DAYS` (moved from `cashflow`, which imports them and never the other way),
   `freshness(as_of)`, `customer_statement(code)` and `customer_totals()`. No other module queries the
   chaseable population. `cashflow` keeps the dashboard aggregates (`ar_aging`, `ar_due_buckets`,
   `bsn_ar_excluded*`), which read the imported predicate.
2. **Keyed by customer code only.** `customer_statement` matches `TRIM(customer_code)`. A blank-code
   snapshot row still counts in `customer_totals`, grouped by its name with no code, and renders on `/ar`
   without a link. Name keys are gone from AR (no live row needs one: 0 blank codes and 0 log rows on
   the 2026-09-30 snapshot).
3. **One age base.** `age_days` counts from the snapshot date on every page (decision aA).
   `/express/ar/customer` used today's date and is the one page whose ages change.
4. **The statement keeps ADR 0012's list choice as a named field.** `chaseable` is every row the
   predicate keeps; `bills` is those with `outstanding > 0`, what a bill list renders; `total` is the net
   of `chaseable`, credits included; `excluded` is every other snapshot row, with `excluded_by`.
5. **Every page sorts in its own view.** The statement returns rows in snapshot order; each page keeps
   its row order by sorting there.
6. **The sales trip shows chaseable.** `/m/sales-trip` reads `customer_totals` by code and carries the
   stale banner. The marketplace exclusion stays.
7. **The dunning page and log form take a code.** A key is a code when the snapshot, the sales ledger,
   the log or the customer master carries it as one. Any other key gets 404 on GET and a refusal on
   POST, so no `ar_followup_log` row is written without a code.
8. **The `/ar` overview drops its Sendy-ledger card.** The reconcile tab already shows the ledger total.

## Rejected

- A third named AR population for the Sendy ledger: two figures that can disagree on chase screens is
  the defect ADR 0012 closed.
- Housing chaseable in `ar_followup.py` (that module is the dunning log) or in `cashflow.py` (the
  dashboard).
- Moving four pages and leaving the call card: one page would keep assembling AR on its own.
- Keeping name keys inside the new module, and keeping `get_customer_debt_summary` beside
  `customer_totals` (a second per-customer total keyed by the raw code).
- Per-page age bases.
- Moving the overview ledger card to the reconcile tab, which already has one.
- Merging every population SQL fragment into one file.

## Consequences

- A new chase page calls `customer_statement` or `customer_totals`; a SELECT on `express_ar_outstanding`
  outside `ar_statement.py` and `cashflow.py` is the smell.
- `tests/test_ar_chaseable_population.py` bans the hand-typed write-off clause in every consumer module
  and pins `cashflow.BSN_AR_PREDICATE is ar_statement.BSN_AR_PREDICATE`.
- A `/call/<bill name>` card whose name carries two codes shows no AR block. Trunk showed the AR of
  whichever code the name resolver picked.
- Receipt status on Sendy-ledger invoices is a separate question and gets its own module and ADR.
