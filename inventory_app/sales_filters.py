"""Canonical answer to "which sales_transactions rows are revenue?".

Every page that reports money off `sales_transactions` must apply the SAME
exclusions, or /revenue, /accounting and the customer pages quietly disagree.
That has already happened once (the /accounting v2 double-subtraction of
returns, 2026-07-21), which is why the rule now is: one definition, imported.

Exclusions, in the order they were introduced:
  doc_base LIKE 'SR%'   sales returns / credit notes
  doc_base LIKE 'HS%'   historical opening balances, not trade
  excludes_revenue      documents invoiced in error — no sale ever happened
                        (migration 142; today: the three วรสวัสดิ์ giveaway
                        invoices). Distinct from bad debt, which WAS a sale
                        and keeps its revenue.

⚠ COGS is deliberately NOT filtered by `excludes_revenue`. A giveaway's goods
really did leave the warehouse, so their cost is a real expense. Removing the
revenue while keeping the cost is what makes gross profit come out right
(the month drops by the full invoiced amount). Filtering both would silently
hand the margin back.

⚠ LOAD-BEARING: `ar_writeoffs.doc_no` must stay NOT NULL (migration 095). A
single NULL in the subquery makes `NOT IN (SELECT ...)` evaluate to NULL for
every row, and every revenue figure in the app would silently become 0. Same
hazard cashflow.py documents for its own ar_writeoffs subquery.

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
    """Full revenue-row filter: excludes returns, opening balances, and
    documents invoiced in error. Use for any SUM(net) presented as revenue."""
    p = '{}.'.format(alias) if alias else ''
    return ("{p}doc_base IS NOT NULL "
            "AND {p}doc_base NOT LIKE 'SR%' "
            "AND {p}doc_base NOT LIKE 'HS%' "
            "AND {not_a_sale}"
            .format(p=p, not_a_sale=not_a_sale_clause(alias)))
