"""Canonical answer to "which sales_transactions rows are revenue?".

Every page that reports money off `sales_transactions` must apply the SAME
exclusions, or /revenue, /accounting and the customer pages quietly disagree.
That has already happened once (the /accounting v2 double-subtraction of
returns, 2026-07-21), which is why the rule now is: one definition, imported.

Exclusions, in the order they were introduced:
  doc_base LIKE 'SR%'   sales returns / credit notes
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
and has its own single definition here too: purchase_net_sql() (#494).

⚠ A THIRD, unrelated question lives here too: how much WE bought from a
SUPPLIER, off `purchase_transactions` (a different table from the two
questions above, both of which read `sales_transactions`). GR docs
(ใบลดหนี้ซื้อ, purchase returns/credit notes) are stored with POSITIVE
net/qty by the same importer convention as SR — see supplier_net_sql() and
supplier_qty_sql() (#591).

Python 3.9 — no `X | None` syntax.
"""

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
            "AND {p}doc_base NOT LIKE 'SR%' "
            "AND {not_a_sale}"
            .format(p=p, not_a_sale=not_a_sale_clause(alias)))


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
    return ("CASE WHEN {p}doc_base LIKE 'SR%' THEN -{p}net ELSE {p}net END"
            .format(p=p))


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
    window in `blueprints/bsn.py::mapping`. `alias` as for not_a_sale_clause()."""
    p = '{}.'.format(alias) if alias else ''
    return "{p}doc_base NOT LIKE 'GR%'".format(p=p)


def supplier_net_sql(alias=''):
    """SQL expression: one purchase_transactions line's share of ยอดซื้อ from a
    supplier, NET of returns (#591) — a GR line is negated. Wrap in SUM()
    yourself, same contract as purchase_net_sql(). `alias` as for
    not_a_sale_clause()."""
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {p}doc_base LIKE 'GR%' THEN -{p}net ELSE {p}net END"
            .format(p=p))


def supplier_qty_sql(alias=''):
    """Same as supplier_net_sql() but for qty — a GR's returned quantity must
    also net OUT of a supplier's purchased quantity, not add to it."""
    p = '{}.'.format(alias) if alias else ''
    return ("CASE WHEN {p}doc_base LIKE 'GR%' THEN -{p}qty ELSE {p}qty END"
            .format(p=p))


# ── COGS: the bill's unit is not the product's unit ──────────────────────────
# `sales_transactions.qty` is denominated in the unit written on the BILL
# (โหล, กล่อง, ซอง); `products.cost_price` is per `products.unit_type`. Every
# COGS over sales lines must convert first, the way the stock ledger already
# does — prod IV6901440-1 sold 16 โหล of pid 134 and `transactions` id 358869
# moved -192 ตัว, while /accounting costed that line at 16 × ฿12. Multiplying
# raw qty by cost_price understated prod COGS by ฿189,535 over Jan–Aug 2026.
#
# One definition, imported — the same reason the revenue filter lives here.


def unit_conversion_join(st='st', uc='uc'):
    """LEFT JOIN pairing each sales line with the ratio for its OWN bill unit."""
    return ("LEFT JOIN unit_conversions {uc} ON {uc}.product_id = {st}.product_id "
            "AND {uc}.bsn_unit = {st}.unit".format(st=st, uc=uc))


def base_qty_sql(st='st', p='p', uc='uc'):
    """SQL expression: one line's qty expressed in the product's BASE unit.

    Mirrors price_lookup._bill_ratio, the resolver's own bill-unit lookup,
    including its short-circuit: a bill unit equal to the base unit is ratio
    1 whatever a unit_conversions row for that unit says.

    It diverges in one place, deliberately. _bill_ratio returns None for a
    unit with no row and the price resolver SKIPS that bill; COGS costs it at
    ratio 1 instead, because dropping the line would understate COGS further.
    Pair this with unratioed_line_sql() so the page can disclose those lines.
    """
    return ("({st}.qty * CASE WHEN COALESCE({st}.unit, '') = '' "
            "OR COALESCE({st}.unit, '') = COALESCE({p}.unit_type, '') "
            "THEN 1.0 ELSE COALESCE({uc}.ratio, 1.0) END)".format(st=st, p=p, uc=uc))


def unratioed_line_sql(st='st', p='p', uc='uc'):
    """SQL expression: 1 when a mapped line's bill unit has no ratio, so
    base_qty_sql() fell back to 1 and that line's cost is understated."""
    return ("CASE WHEN {p}.id IS NOT NULL AND {uc}.ratio IS NULL "
            "AND COALESCE({st}.unit, '') <> '' "
            "AND COALESCE({st}.unit, '') <> COALESCE({p}.unit_type, '') "
            "THEN 1 ELSE 0 END".format(st=st, p=p, uc=uc))
