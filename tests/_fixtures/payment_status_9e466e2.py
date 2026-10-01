"""Frozen trunk receipt-status logic, copied verbatim from
inventory_app/models/payments.py at origin/main
9e466e201945cf6d96474ad58464fdd9518cf6b8 (lines 348-489).

Only test_receipt_status's differential test imports this. It must never be
edited to follow the app: it is the oracle the new receipt_status module is
compared against, so a change here makes the test compare the code with itself.
"""

from database import get_connection
import document_kind
import vat_math


# ── Active-receipt payment status (Findings 3 & 4) ──────────────────────────
#
# ONE definition of "this invoice has at least one ACTIVE (non-cancelled)
# receipt link", shared by every legacy status reader so they cannot drift.
# Two properties are load-bearing:
#
#   * DISTINCT collapses the several links one invoice can legitimately carry
#     (183 invoices live), so joining this can never multiply a bill's count or
#     its baht. The old link-grain join reported 7,908 paid out of 7,899 total
#     bills and ฿24,129,799.79 paid (a payment rate above 100%); the same data
#     at one-invoice grain is 7,723 paid / ฿21,499,826.74.
#
#   * the JOIN to received_payments carries `cancelled = 0` INSIDE the CTE. The
#     old idiom left-joined received_payments and then tested `pi.doc_no IS NOT
#     NULL` — which stays non-null even when the cancelled-receipt join
#     produced no row, so a cancelled receipt still marked the invoice paid.
_ACTIVE_PAID_DOCS_CTE = """
    active_paid_docs AS (
        SELECT DISTINCT pi.doc_no
          FROM paid_invoices pi
          JOIN received_payments rp ON rp.id = pi.re_id
         WHERE rp.cancelled = 0
    )
"""

# DISPLAY ONLY — the latest active receipt per invoice, one row per doc_no so
# it cannot multiply the bill either. Taking the whole row of the newest
# receipt (rather than MAX(date), MAX(re_no) independently) keeps the shown
# date and receipt number from belonging to two different receipts.
_ACTIVE_PAYMENT_DISPLAY_CTE = """
    active_payment_display AS (
        SELECT doc_no, paid_date, re_no FROM (
            SELECT pi.doc_no,
                   rp.date_iso AS paid_date,
                   rp.re_no,
                   ROW_NUMBER() OVER (PARTITION BY pi.doc_no
                                      ORDER BY rp.date_iso DESC, rp.id DESC) AS rn
              FROM paid_invoices pi
              JOIN received_payments rp ON rp.id = pi.re_id
             WHERE rp.cancelled = 0
        ) WHERE rn = 1
    )
"""


def get_payment_status(status='all', search='', date_from='', date_to='', page=1, per_page=50):
    """Get IV invoices with payment status.
    Uses pre-computed doc_base column + index for performance.
    """
    conn = get_connection()

    # HS is paid on the spot, never a receivable (#514).
    conds = ["st.doc_base IS NOT NULL", document_kind.not_return_sql('st', 'sales'), "st.doc_base NOT LIKE 'HS%'"]
    params = []

    if search:
        conds.append("(st.doc_base LIKE ? OR st.customer LIKE ?)")
        params += [f'%{search}%', f'%{search}%']
    if date_from:
        conds.append("st.date_iso >= ?"); params.append(date_from)
    if date_to:
        conds.append("st.date_iso <= ?"); params.append(date_to)

    paid_filter = ''
    if status == 'paid':
        paid_filter = 'HAVING is_paid = 1'
    elif status == 'unpaid':
        paid_filter = 'HAVING is_paid = 0 AND total_net > 0'
    else:
        paid_filter = 'HAVING total_net > 0'

    where = ' AND '.join(conds)

    sql = f"""
        WITH {_ACTIVE_PAID_DOCS_CTE}, {_ACTIVE_PAYMENT_DISPLAY_CTE}
        SELECT
            st.doc_base,
            MIN(st.date_iso) AS bill_date,
            st.customer,
            SUM({vat_math.cash_sql('st')}) AS total_net,
            MAX(CASE WHEN apd.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
            MAX(apay.paid_date) AS paid_date,
            MAX(apay.re_no) AS re_no
        FROM sales_transactions st
        LEFT JOIN active_paid_docs apd ON apd.doc_no = st.doc_base
        LEFT JOIN active_payment_display apay ON apay.doc_no = st.doc_base
        WHERE {where}
        GROUP BY st.doc_base
        {paid_filter}
        ORDER BY bill_date DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        WITH {_ACTIVE_PAID_DOCS_CTE}
        SELECT COUNT(*) FROM (
            SELECT st.doc_base,
                MAX(CASE WHEN apd.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
                SUM({vat_math.cash_sql('st')}) AS total_net
            FROM sales_transactions st
            LEFT JOIN active_paid_docs apd ON apd.doc_no = st.doc_base
            WHERE {where}
            GROUP BY st.doc_base
            {paid_filter}
        )
    """
    total = conn.execute(count_sql, params).fetchone()[0]
    conn.close()
    return rows, total


def get_payment_summary():
    """Quick stats for payment status page.

    Invariants (Finding 3): `paid_count + unpaid_count == total_bills` and
    `paid_count <= total_bills` hold structurally — the inner query is already
    one row per doc_base, and active_paid_docs is one row per invoice, so no
    join here can multiply a bill.
    """
    conn = get_connection()
    row = conn.execute(f"""
        WITH {_ACTIVE_PAID_DOCS_CTE}
        SELECT
            COUNT(*) AS total_bills,
            SUM(CASE WHEN apd.doc_no IS NOT NULL THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN apd.doc_no IS NULL THEN 1 ELSE 0 END) AS unpaid_count,
            SUM(CASE WHEN apd.doc_no IS NOT NULL THEN st.net ELSE 0 END) AS paid_amount,
            SUM(CASE WHEN apd.doc_no IS NULL THEN st.net ELSE 0 END) AS unpaid_amount
        FROM (
            SELECT doc_base,
                   SUM({vat_math.cash_sql()}) AS net
            FROM sales_transactions
            WHERE doc_base IS NOT NULL AND {document_kind.not_return_sql('', 'sales')} AND doc_base NOT LIKE 'HS%'
            -- HS is paid on the spot, never a receivable (#514)
            GROUP BY doc_base
            HAVING SUM({vat_math.cash_sql()}) > 0
        ) st
        LEFT JOIN active_paid_docs apd ON apd.doc_no = st.doc_base
    """).fetchone()
    conn.close()
    return row
