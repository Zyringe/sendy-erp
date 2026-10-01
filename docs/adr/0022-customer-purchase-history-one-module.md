# ADR 0022 — A customer's purchase history has one home: `purchase_history.py`

Status: Accepted · 2026-10-01 (decisions A1, B1, C1, D1, E1: Put, 2026-09-30)

## Context

Five screens show what a customer bought: the desktop customer page, the `/customers` list, the mobile
customer page, the call card with the `/call` list, and the mobile sales trip. `CONTEXT.md` ("Customer
page") defined the figures, but the code shared only one-line SQL fragments, so each screen wrote its own
query and four census tests (#494, #513, #554, #627) policed the result by counting aggregates per
function. The call card had drifted: its header was keyed on ONE bill name, its product table counted
credit notes and freebie-only bills as purchases, and it read documents invoiced in error.

## Decision

1. **`inventory_app/purchase_history.py` owns the figures.** `history(key)` for one customer,
   `histories()` for every customer, `totals()` / `documents()` / `products()` for a screen that needs one
   piece. Surfaces read them and hold no aggregate of their own.
2. **The key is the customer code.** A sales row with no code takes the one code that carries the same
   exact bill name (A1, `customer_key_sql`, read-time, nothing written); with no such code, or two, the key
   is the bill name and the row stays an orphan: never guessed. `customer_key_sql` is the ONLY statement of
   that rule. `history()` asks it once per `history()` call for the bill names that resolve to the key and filters on
   them as bound parameters. The call card follows the same rule: a bill name that is itself a `/call` entry is that
   entry, not the name resolver's first code (`ar_followup._customer_group` still takes `LIMIT 1` for AR). One guess remains: a typed or bookmarked URL with a two-code bill name that has NO code-less rows is not a `/call` entry, and the card still uses the resolver's first code as its history key. No link in the app produces such a URL.
3. **Windows are explicit per fact.** `history(date_from, date_to)` bounds every field except `winback`,
   which is always all-time. `histories(total_since)` bounds `purchase_total` only. The rule lives in the
   module, not at the call site.
4. **Purchase is the purchase population.** ครั้งที่ซื้อ, ซื้อล่าสุด and ซื้อครั้งแรก (E1) read
   `price_lookup.purchase_population_filter`; a credit note, a freebie-only bill and a document invoiced in
   error are never a purchase. The call card's ล่าสุด is the last paid purchase (B1), a product only ever
   returned or given free is not a ซื้อประจำ row (C1), and 01อ35's giveaway bills stay out of ซื้อรวม qty
   (D1). The call card's clearance panel is deliberately unchanged (Put, 2026-09-30): it seeds from
   everything the customer received.
5. **One test enforces it.** `tests/test_purchase_history_must_use.py` says: a customer-keyed history shape
   (last date, document count, money or quantity sum, purchase-population call) outside the module is
   declared with a reason; every surface in `SURFACES_MUST_CALL` calls the module and holds no shape of its
   own; the module keeps using the shared helpers. The #494/#513/#627 censuses keep the non-customer
   readers (AR, revenue, the trade screens, price evidence). Every census's customer-key regex includes
   `customer_key_sql`; a query keyed only through it used to be invisible.

## Consequences

- A new customer-history figure goes into `purchase_history.py`. Adding it to a surface turns the must-use
  test red naming the function.
- If an import ever gives a bill name that A1 resolved a second code, that name's credit notes detach and
  the twin's ยอดซื้อรวม jumps back up, silently. By design (never guess); worth a data-quality alert later.
- `history()` filters on the key, which no index serves: each call scans `sales_transactions` (25-36 ms
  on 20.6k rows). A loop over keys must use `histories()`. Upgrade path: an expression index on
  `customer_key_sql('')`.
- Not covered by the test: SQL built from separate variables, and a figure computed in Python from fetched
  rows.
