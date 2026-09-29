"""Canonical answer to "which sales_transactions rows are revenue?".

Every page that reports money off `sales_transactions` must apply the SAME
exclusions, or /revenue, /accounting and the customer pages quietly disagree.
That has already happened once (the /accounting v2 double-subtraction of
returns, 2026-07-21), which is why the rule now is: one definition, imported.

Exclusions, in the order they were introduced:
  SR documents          sales returns / credit notes (document_kind owns the prefix)
  excludes_revenue      documents invoiced in error — no sale ever happened
                        (migration 142; today: the three วรสวัสดิ์ giveaway
                        invoices). Distinct from bad debt, which WAS a sale
                        and keeps its revenue.

⚠ HS (doc_base LIKE 'HS%') is NOT excluded, on purpose: an HS document is a
cash sale (ขายสด) in the BSN5657 Express book, not a historical opening
balance — that earlier reading was an assumption and was wrong (#514). It
posts real money to the GL sales account (41-01, same as IV) and its goods
really left the warehouse, so it counts as revenue AND as COGS. AR/
settlement paths (payments_alloc._settlement_rows, models/payments.py,
blueprints/mobile.py's per-customer "outstanding") keep excluding it
deliberately — a cash sale is paid on the spot, so it is never a
receivable. That is a different question from this filter and is NOT
revisited here.

⚠ COGS is deliberately NOT filtered by `excludes_revenue`. A giveaway's goods
really did leave the warehouse, so their cost is a real expense. Removing the
revenue while keeping the cost is what makes gross profit come out right
(the month drops by the full invoiced amount). Filtering both would silently
hand the margin back.

⚠ LOAD-BEARING: `ar_writeoffs.doc_no` must stay NOT NULL (migration 095). A
single NULL in the subquery makes `NOT IN (SELECT ...)` evaluate to NULL for
every row, and every revenue figure in the app would silently become 0. Same
hazard cashflow.py documents for its own ar_writeoffs subquery.

A customer's purchase total (ยอดซื้อรวม) is a different question from revenue
and has its own single definition here too: purchase_net_sql() (#494). The
trade screens' ยอดขาย (#627) is the same per-line figure summed over every
customer: sales_net_sql() / sales_qty_sql().

⚠ A THIRD, unrelated question lives here too: how much WE bought from a
SUPPLIER, off `purchase_transactions` (a different table from the two
questions above, both of which read `sales_transactions`). GR docs
(ใบลดหนี้ซื้อ, purchase returns/credit notes) are stored with POSITIVE
net/qty by the same importer convention as SR — see supplier_net_sql() and
supplier_qty_sql() (#591).

Python 3.9 — no `X | None` syntax.
"""
# The COGS base-qty SQL lives in unit_conversion (ADR 0019). Re-exported so
# the dated rebase scripts and the tests that import it from here keep working.
from unit_conversion import base_qty_sql, unit_conversion_join, unratioed_line_sql  # noqa: F401
# Which prefix is a return (SR sales / GR purchase) is document_kind's to say;
# this module never types it.
from document_kind import is_return_sql, not_return_sql

# Documents that were invoiced but are not sales. Joined on the BASE document
# number: sales_transactions carries a per-line '-N' suffix in doc_no, while
# ar_writeoffs stores the base document.
_NOT_A_SALE_SUBQUERY = "SELECT doc_no FROM ar_writeoffs WHERE excludes_revenue = 1"


def not_a_sale_clause(alias=''):
    """SQL predicate keeping only rows that represent a real sale.

    `alias` is the table alias used in the caller's FROM clause ('' when the
    table is unaliased), e.g. not_a_sale_clause('st') -> "COALESCE(st.doc_base,
    st.doc_no) NOT IN (...)".
    """
    p = '{}.'.format(alias) if alias else ''
    return ("COALESCE({p}doc_base, {p}doc_no) NOT IN ({sub})"
            .format(p=p, sub=_NOT_A_SALE_SUBQUERY))


def revenue_filter(alias=''):
    """Full revenue-row filter: excludes returns and documents invoiced in
    error. Counts HS (cash sales, #514) same as IV. Use for any SUM(net)
    presented as revenue."""
    p = '{}.'.format(alias) if alias else ''
    return ("{p}doc_base IS NOT NULL "
            "AND {not_return} "
            "AND {not_a_sale}"
            .format(p=p, not_return=not_return_sql(alias, 'sales'),
                    not_a_sale=not_a_sale_clause(alias)))


def return_filter(alias=''):
    """revenue_filter's other half: the credit notes it excludes, over the same
    real documents. `revenue_filter OR return_filter` is every real sale-side
    document line, and the two never overlap.

    A caller that only wants revenue keeps revenue_filter. This exists for the
    surfaces that must SUBTRACT a return rather than drop it (#646) and so need
    to name the rows being subtracted. Use purchase_net_sql/sales_qty_sql when
    one signed expression over the un-split population is enough."""
    p = '{}.'.format(alias) if alias else ''
    return ("{p}doc_base IS NOT NULL "
            "AND {is_return} "
            "AND {not_a_sale}"
            .format(p=p, is_return=is_return_sql(alias, 'sales'),
                    not_a_sale=not_a_sale_clause(alias)))


def purchase_net_sql(alias=''):
    """SQL expression: one line's share of a customer's ยอดซื้อรวม (#494).

    Before VAT (`net` is always ex-VAT, see vat_math), and NET of returns: a
    credit-note (SR) line is stored with a POSITIVE net, so it is negated here.
    Before #494 the header summed a bare `net` and ADDED returns (prod 56ช001:
    ฿94,140.00 against the ฿50,460.00 its own document list sums to).

    HS cash sales count like invoices, as the customer page's document list
    shows them — same stance as revenue_filter() since #514. This function
    predates that fix and stays separate anyway: it is a per-row CASE
    expression, not a WHERE predicate, and doesn't fold in
    `doc_base IS NOT NULL` / not_a_sale_clause() the way revenue_filter()
    does — callers add those themselves (see below).

    Per-row expression only, like vat_math.cash_sql(): wrap it in SUM()
    yourself, and keep documents invoiced in error out with
    not_a_sale_clause() in the same query's WHERE. `alias` as for
    not_a_sale_clause().
    """
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {ret} THEN -{p}net ELSE {p}net END"
            .format(p=p, ret=is_return_sql(alias, 'sales')))


# ── ยอดขาย on the trade screens, NET of returns (#627) ───────────────────────
#
# /sales, /trade-dashboard, /products/<id>/trade and the call card's product
# ordering. Before #627 /sales and the product page summed a bare `net`, so a
# credit note (stored POSITIVE) was ADDED as a sale, while the dashboard
# dropped SR entirely: prod 2026 Jan-Sep read ฿2,900,927.44 vs ฿2,886,748.94,
# the gap being exactly the 16 SR lines. Put's ruling 2026-09-22: subtract
# a return, the same rule as ยอดซื้อ (#591) and ยอดซื้อรวม (#494). A product
# or a month may go negative. This is NOT revenue: revenue_filter() still
# leaves returns out of /accounting and /revenue (GL 41-01).
#
# Which documents count is the caller's WHERE: /sales and the product page
# keep every document (option ก); only the dashboard adds not_a_sale_clause().

def sales_net_sql(alias=''):
    """SQL expression: one sales line's share of ยอดขาย on the trade screens,
    NET of returns: a credit-note (SR) line is negated. Wrap in SUM()
    yourself. It is the same per-line figure a customer's ยอดซื้อรวม sums, so
    it delegates to purchase_net_sql(): one definition of the SR sign for
    both sides of a sale."""
    return purchase_net_sql(alias)


def sales_qty_sql(alias=''):
    """Same as sales_net_sql() but for qty: a returned quantity nets OUT of
    what was sold (SR qty is stored positive too)."""
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {ret} THEN -{p}qty ELSE {p}qty END"
            .format(p=p, ret=is_return_sql(alias, 'sales')))


# ── purchase_transactions: how much WE bought from a supplier (#591) ─────────
#
# GR (ใบลดหนี้ซื้อ, purchase return/credit note) rows are stored with POSITIVE
# net/qty, same importer convention as SR — the DBF adapter never signs them
# (test_express_dbf_source.py's own docstring: "APRCPIT stores a GR credit
# ... unsigned, like ARRCPIT's SR"). Before #591 no aggregate over
# purchase_transactions excluded or negated GR, so a raw SUM(net) ADDED
# returns onto purchases: prod 2026 Jan-Sep read ฿699,280.59 raw vs
# ฿693,428.57 net of returns, and /supplier/ไพบูลย์'s header read ฿462,473 vs
# the true ฿426,473. Put's ruling 2026-09-19: ยอดซื้อ = net of returns.

def not_a_purchase_return_clause(alias=''):
    """SQL predicate keeping only rows that represent a real purchase: excludes
    GR entirely. Use where a return should not appear at all (a single "latest
    purchase" row, not a sum) — `bsn_suggest._latest_purchase` and the matching
    window in `blueprints/bsn.py::mapping`. `alias` as for not_a_sale_clause().

    NULL-safe on purpose: `doc_base` is a nullable column, and a bare
    `doc_base NOT LIKE 'GR%'` in a WHERE clause evaluates to NULL — not TRUE —
    for a NULL doc_base, which would silently drop a real purchase with
    unknown doc_base from the "latest purchase" window instead of just
    failing to recognize it as GR. (supplier_net_sql()/supplier_qty_sql()'s
    CASE below does not need this: its ELSE branch already falls through
    correctly on NULL.)"""
    return not_return_sql(alias, 'purchase', null_safe=True)


def supplier_net_sql(alias=''):
    """SQL expression: one purchase_transactions line's share of ยอดซื้อ from a
    supplier, NET of returns (#591) — a GR line is negated. Wrap in SUM()
    yourself, same contract as purchase_net_sql(). `alias` as for
    not_a_sale_clause()."""
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {ret} THEN -{p}net ELSE {p}net END"
            .format(p=p, ret=is_return_sql(alias, 'purchase')))


def supplier_qty_sql(alias=''):
    """Same as supplier_net_sql() but for qty — a GR's returned quantity must
    also net OUT of a supplier's purchased quantity, not add to it."""
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {ret} THEN -{p}qty ELSE {p}qty END"
            .format(p=p, ret=is_return_sql(alias, 'purchase')))


# ── COGS: WHICH cost, as well as how much of it ──────────────────────────────
# `products.cost_price` is the product's cost RIGHT NOW, so costing a sales
# line with it makes a closed month a live re-derivation: measured 2026-09-18,
# a closed nine-month ต้นทุนขาย moved ฿289.46 between two reads on one morning
# with no sale and no purchase involved. From COGS_HISTORICAL_FROM a line is
# costed at the carrying amount on its own sale date instead, read from
# `product_cost_ledger`'s running `wacc_after` (ADR 0015).
#
# ⚠ This does NOT make a closed month immutable, and the page must not say it
# does. It makes it immune to `cost_price` drift, which is what actually moved
# it. The ledger is DELETEd and rebuilt per product by `wacc.py`, so a change
# to historical `transactions` (a re-import, a unit rebase) or to the walk
# itself still moves history — which is ADR 0015 decision 3's disclosed
# correction, not the silent drift this replaces. Measured 2026-09-20 on a
# copy of prod: rebuilding all 1,755 products moved closed-month COGS by
# ฿0.00, with 1,748 reproducing exactly and 7 differing by <= ฿0.000021 of
# float re-accumulation noise.

# Must equal models.wacc._WACC_INITIAL_DATE — the ledger's `INITIAL` rows reset
# every product's cost on exactly this date, which is why the basis before it
# is unreachable. Pinned by tests/test_accounting_cogs_basis.py; importing
# models.wacc here would make sales_filters depend on the whole model layer.
COGS_HISTORICAL_FROM = '2026-03-03'


def cogs_unit_cost_sql(st='st', p='p'):
    """SQL expression: the cost per BASE unit to charge one sales line.

    Two clauses in the subquery are not obvious and both are load-bearing.

    `event_date >= COGS_HISTORICAL_FROM` floors the lookup at the cutover.
    `wacc.py` writes an `INITIAL` row only when `opening_cost > 0`, so 871
    products on prod have none and a post-cutover sale would otherwise resolve
    straight back to a 2024 `PURCHASE` row — the basis ADR 0015 says it
    discarded. 51 lines on prod, worth ฿0.67.

    `wacc_after > 0` skips the negative-stock sentinel. When stock goes
    negative the walk freezes the running average at 0 and the write guard at
    `wacc.py` (`if current_wacc and current_wacc > 0`) deliberately leaves
    `cost_price` alone, so for that class the ledger is the WORSE source (prod
    pid 714: ledger 0.0 against `cost_price` 7.0; 182 pre-cutover rows carry
    0). Put ruled 2026-09-20 that the recorded cost wins, because goods that
    left the warehouse had a cost. Worth ฿336.00.

    Returns NULL only when there is no cost anywhere, so `IS NULL` on this
    expression is the page's `no_cost_lines` and `= 0` is `zero_cost_lines`.
    Pair it with no_ledger_line_sql() to disclose the fallbacks.

    The ORDER BY is byte-identical to `wacc.get_current_wacc`'s, on purpose:
    ids are assigned in walk order within a product, so the last row of a date
    is that day's closing WACC after all of that day's INs, which is the same
    INs-before-OUTs convention the walk itself uses.
    """
    return ("CASE WHEN {st}.date_iso >= '{cut}' THEN COALESCE(("
            "SELECT pcl.wacc_after FROM product_cost_ledger pcl"
            " WHERE pcl.product_id = {st}.product_id"
            " AND pcl.event_date <= {st}.date_iso"
            " AND pcl.event_date >= '{cut}'"
            " AND pcl.wacc_after > 0"
            " ORDER BY pcl.event_date DESC, pcl.id DESC LIMIT 1"
            "), {p}.cost_price) ELSE {p}.cost_price END"
            .format(st=st, p=p, cut=COGS_HISTORICAL_FROM))


def no_ledger_line_sql(st='st', p='p'):
    """SQL expression: 1 when a MAPPED post-cutover line found no usable ledger
    row and fell back to `cost_price`, so the page can disclose it.

    Guarded on `{p}.id IS NOT NULL` the way unratioed_line_sql() is: an
    unmapped line has no product to have a ledger for, and counting it here as
    well as in `no_cost_lines` shows two counts for one line.

    ⚠ ADR 0015 says these lines' "cost is ฿0 regardless". That is measurably
    false — they carry ฿3,080 of real cost on prod — and the ADR now carries a
    correction. Costing them at zero would trade a ฿1,876 problem for a ฿3,080
    one, so they keep `cost_price` and are counted instead.

    ⚠ This count can shrink without anyone touching cost data: `wacc.py`'s
    `get_current_wacc` and `get_cost_history` rebuild a missing ledger and
    commit, and both are reachable from a product page view. Measured
    2026-09-20 on a copy of prod: recalculating all 12 exposed products moved
    closed-month COGS by ฿0.00, because they have no qualifying IN rows and
    `cost_price = 0`, so they cannot acquire a ledger this way today.
    """
    return ("CASE WHEN {p}.id IS NOT NULL AND {st}.date_iso >= '{cut}'"
            " AND NOT EXISTS (SELECT 1 FROM product_cost_ledger pcl"
            " WHERE pcl.product_id = {st}.product_id"
            " AND pcl.event_date <= {st}.date_iso"
            " AND pcl.event_date >= '{cut}'"
            " AND pcl.wacc_after > 0) THEN 1 ELSE 0 END"
            .format(st=st, p=p, cut=COGS_HISTORICAL_FROM))
