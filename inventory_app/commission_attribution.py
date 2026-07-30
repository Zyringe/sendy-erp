"""Canonical answer to "which salesperson does this sale belong to?".

Commission is attributed from `received_payments.salesperson` — the rep code
Express stamps on the RE document. When a rep stops servicing a customer,
Express keeps stamping the old code, so the departed rep keeps earning. The
`commission_customer_reassign` table (migration 143) records the override, and
THIS module is the single definition of how to apply it.

One definition, imported — same rule as `sales_filters.py`, and for the same
reason: the rep code is read at six sites across `commission.py` and
`blueprints/accounting.py`, four of them on the money path, and three of those
build their own query rather than going through `_BASE_QUERY`. Four copies of
this logic would drift, and the drift is invisible (two screens one click
apart, disagreeing by exactly the amount the rule exists to move).

Semantics — keyed on the INVOICE date, not the receipt date
    A rule applies to a sales document when
        sales_transactions.customer_code = customer_code
    AND sales_transactions.date_iso     >= effective_from

    "He sold it, he earns it": an order written BEFORE the cut keeps paying the
    original rep whenever it is eventually collected, so no already-paid cycle
    is restated. (Put, 2026-07-30.)

    Where several rules exist for one customer, the applicable one is the
    LATEST `effective_from` at or before the document's date, so a customer can
    move 31 -> 00 now and 00 -> someone else later with history staying correct.

⚠ LOAD-BEARING: every expression here is wrapped in COALESCE(..., <original>).
A customer with no rule, an inactive rule, a rule dated after the document, or
an empty `customer_code` (3 such sales rows exist) must all fall through to the
rep Express stamped. A NULL leaking out instead would drop the line from every
per-salesperson aggregate silently.

Safe to interpolate: both helpers take only SQL *aliases and expressions*
chosen by the caller at import time, never user input. No parameters are
consumed, so callers' positional placeholders are unaffected.

Uniformity assumption (verified 2026-07-30 across all 8,047 documents): a
`doc_base` carries exactly one `customer_code` and one `date_iso`. That is what
makes the line-level and document-level forms below equivalent.

Python 3.9 — no `X | None` syntax.
"""

# Latest active rule at or before a given (customer, date). Callers supply the
# two scalar expressions; `ORDER BY effective_from DESC LIMIT 1` is what makes
# "latest rule wins" true rather than "any matching rule wins".
_RULE_SUBQUERY = """(
            SELECT r.to_salesperson
              FROM commission_customer_reassign r
             WHERE r.is_active = 1
               AND r.customer_code  = {customer_expr}
               AND r.effective_from <= {date_expr}
             ORDER BY r.effective_from DESC
             LIMIT 1
        )"""


def resolved_salesperson(customer_expr, date_expr, original_expr):
    """Resolved rep code, given SQL expressions for the document's customer
    code, its date, and the rep Express stamped.

    Use when the query already has `sales_transactions` in scope:
        resolved_salesperson('es.customer_code', 'es.date_iso',
                             'rcv.salesperson_code')
    """
    return 'COALESCE({rule}, {orig})'.format(
        rule=_RULE_SUBQUERY.format(customer_expr=customer_expr,
                                   date_expr=date_expr),
        orig=original_expr,
    )


def resolved_salesperson_for_doc(doc_expr, original_expr):
    """Resolved rep code when only a bare document number is in scope.

    Resolves the document's customer and date out of `sales_transactions`
    first. `MIN(date_iso)` and `ORDER BY id LIMIT 1` are deterministic tie-
    breaks; per the uniformity check above there is nothing to break.

    Used by the receipt-side queries (`get_invoice_cycle_month`, the receipt
    lookup in `get_invoice_line_breakdown`) which join
    paid_invoices -> received_payments and never touch sales_transactions.
    """
    customer_expr = ('(SELECT st_r.customer_code FROM sales_transactions st_r '
                     'WHERE st_r.doc_base = {doc} ORDER BY st_r.id LIMIT 1)'
                     .format(doc=doc_expr))
    date_expr = ('(SELECT MIN(st_d.date_iso) FROM sales_transactions st_d '
                 'WHERE st_d.doc_base = {doc})'.format(doc=doc_expr))
    return resolved_salesperson(customer_expr, date_expr, original_expr)


def resolved_salesperson_for_receipt(receipt_id_expr, original_expr):
    """Resolved rep for a whole RECEIPT, which may settle several invoices.

    A receipt is only unambiguously reassigned when EVERY invoice it settles
    resolves to the same rep. `MIN(sp) = MAX(sp)` is that unanimity test; a
    receipt straddling an `effective_from` yields NULL and falls back to the
    Express-stamped code rather than silently picking one side.

    No such straddling receipt exists today (checked 2026-07-30: multi-invoice
    receipts go up to 4 invoices, all on one side of every active cut), but one
    old + one new invoice settled together is an ordinary thing to do, so this
    fails safe instead of assuming it away.

    Display use only — the commission engine attributes per sales LINE and
    never needs to collapse a receipt to one rep.
    """
    per_invoice = resolved_salesperson_for_doc('pi_r.doc_no', original_expr)
    return """COALESCE((
            SELECT CASE WHEN MIN(x.sp) = MAX(x.sp) THEN MIN(x.sp) END
              FROM (SELECT {per_invoice} AS sp
                      FROM paid_invoices pi_r
                     WHERE pi_r.re_id = {receipt}
                       AND pi_r.doc_kind = 'IV'
                       AND pi_r.amount IS NOT NULL AND pi_r.amount <> 0) x
        ), {orig})""".format(per_invoice=per_invoice,
                             receipt=receipt_id_expr,
                             orig=original_expr)
