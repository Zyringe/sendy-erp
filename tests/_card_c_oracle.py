"""FROZEN oracle for card C: the per-surface SQL exactly as it stood before
`purchase_history` replaced it.

Copied from origin/main c9ef583 (`inventory_app/models/customers.py`, whose
customer-page queries are identical to 4b7b9d3, the pre-card-C tree) and never
edited. The customer-page functions are byte-for-byte those blocks (renamed
without the leading underscore); the last four are the surface queries lifted
out of get_customers / sales_trip / get_call_list with their SQL text unchanged
and their comments dropped. The differential
tests compare the live code against THIS file, because comparing a module with
the code that now calls it would be a module-vs-itself check that cannot fail
(P1 review W3). Do not import anything from `models.customers` here, and do not
"refresh" this file when the app changes: a difference is the finding.

Contents:
  customer_sales_scope, customer_documents, customer_sales_aggregates,
  returns_off_cards, product_rows   -- the customer page (P1 side)
  customers_list_rows               -- get_customers' billing SQL, minus the
                                       region/paging tail
  trip_last_sale, call_spend, call_last_buy -- the trip and /call queries
"""
import document_kind
import price_lookup
import sales_filters
import vat_math


def customer_sales_scope(key_col, key_value, date_from, date_to):
    """WHERE clause + params selecting one customer's sales rows.

    `key_col` is `customer` (the bill name) or `customer_code` (the master code)
    — a literal chosen by the caller at the call site, never user input, so it is
    safe to interpolate. `key_value` stays a bound parameter.
    """
    conds = [f'{key_col} = ?']
    params = [key_value]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    # Documents invoiced in error are not purchases — วรสวัสดิ์ never bought the
    # giveaway, so it must not show on their page either (see sales_filters).
    conds.append(sales_filters.not_a_sale_clause())
    return ' AND '.join(conds), params


def customer_documents(conn, where, params, limit=None):
    """One row per DOCUMENT (doc_base), never per line — the shared grouping
    #493 introduced. `sales_transactions.doc_no` carries a per-line '-N'
    suffix, so grouping by it (the pre-#493 code) produced one "document" per
    LINE: 247 lines on customer 23ท06 read as 247 documents when the real
    count is 66. `_customer_sales_aggregates`'s docs list and the mobile quick
    page (models.get_customer_documents) both call this now, so they cannot
    drift apart again.

    Each row: doc_base, date_iso (latest line date on the doc), item_count
    ('รายการ' — the line count, not a quantity summed across units),
    vat_type (of the doc's lines; ยกเว้น/ไม่บวก VAT are never mixed with แยก
    VAT on one document in practice), total (ยอดรวมเอกสาร — VAT added through
    vat_math for แยก VAT documents, summed; NEGATIVE for a credit note),
    is_credit_note, ref_invoice (the invoice an SR credits, NULL otherwise).
    """
    limit_sql = f'LIMIT {int(limit)}' if limit is not None else ''
    rows = conn.execute(f"""
        SELECT doc_base,
               MAX(date_iso) AS date_iso,
               COUNT(*) AS item_count,
               MAX(vat_type) AS vat_type,
               SUM({vat_math.cash_sql()}) AS raw_total,
               ({document_kind.is_return_sql('', 'sales')}) AS is_credit_note,
               MAX(ref_invoice) AS ref_invoice
        FROM sales_transactions
        WHERE {where}
        GROUP BY doc_base
        ORDER BY date_iso DESC, doc_base
        {limit_sql}
    """, params).fetchall()
    docs = []
    for r in rows:
        d = dict(r)
        total = d.pop('raw_total') or 0
        d['is_credit_note'] = bool(d['is_credit_note'])
        d['total'] = -total if d['is_credit_note'] else total
        docs.append(d)
    return docs


def customer_sales_aggregates(conn, where, params):
    """The four per-customer sales aggregates, defined ONCE.

    The name-keyed and code-keyed summaries below differ only in their WHERE, and
    two copies of these queries drift invisibly — the same reason `sales_filters`
    and `commission_attribution` exist as single definitions.

    `total_net` in summary and monthly is ยอดซื้อรวม: before VAT, credit notes
    subtracted (sales_filters.purchase_net_sql, #494). The summary's
    `total_qty` (จำนวนชิ้น) follows the same rule (sales_qty_sql, #627).

    Returns (summary, top_products, monthly, docs).
    """
    import price_lookup

    summary = dict(conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS doc_count,
               COALESCE(SUM({sales_filters.purchase_net_sql()}), 0) AS total_net,
               COALESCE(SUM({sales_filters.sales_qty_sql()}), 0) AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM sales_transactions
        WHERE {where}
    """, params).fetchone())

    # The PURCHASE population (#493, #513): evidence-filtered lines only — never
    # a credit note (SR), a document invoiced in error, or a free/zero-net line.
    # ONE query answers both figures, so the count and the date can never come
    # to describe different sets of documents: the newest of the documents
    # `purchase_doc_count` counts IS `last_purchase_date`, which is what the
    # call card prints side by side.
    #
    # Deliberately NOT the same thing as `doc_count`/`last_date` above, which
    # stay raw: the customer page labels them จำนวนเอกสาร and the ช่วงเวลา range
    # end, and a credit note IS a document and IS activity. Only a surface that
    # says ซื้อ ("bought") may read these two.
    purchases = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS n,
               MAX(date_iso)            AS d
        FROM sales_transactions
        WHERE {where} AND {price_lookup.purchase_population_filter('')}
    """, params).fetchone()
    summary['last_purchase_date'] = purchases['d']
    summary['purchase_doc_count'] = purchases['n']

    # Money-ordered NET of returns (#627), like the trade screens: the call
    # card's แบรนด์เด่น reads the first name. A MAPPED line groups on
    # product_id alone — a credit note prints `product_name_raw` differently
    # from the invoice it reverses often enough that keeping it in the key
    # left the return as its own negative row (#627 review). An UNMAPPED line
    # has no product to group on and keeps the raw name.
    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, s.product_name_raw) AS name,
               p.id AS product_id,
               s.unit,
               SUM({sales_filters.sales_qty_sql('s')}) AS total_qty,
               SUM({sales_filters.sales_net_sql('s')}) AS total_net,
               COUNT(DISTINCT s.doc_base) AS doc_count
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        GROUP BY s.product_id,
                 CASE WHEN s.product_id IS NULL THEN s.product_name_raw END
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_base) AS doc_count,
               SUM({sales_filters.purchase_net_sql()}) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    # Every document (#493 — no 200-LINE cap cutting off old invoices; the old
    # cap was on `doc_no`, i.e. lines, so it silently dropped whole invoices).
    docs = customer_documents(conn, where, params)

    return summary, top_products, monthly, docs


def returns_off_cards(conn, where, params, cards):
    """฿ of this customer's credit notes that the RENDERED cards do not show.

    #646 nets a return onto its card, but three kinds never reach one: a
    product this customer never bought (no card exists), a return booked in a
    different unit from the purchase (different key), and a card that exists
    but fell outside the top-20 union. Without this the page is silent about
    them, which is the same "disagrees with itself" complaint one level up.
    Measured on prod 2026-09-23: ฿46,365.68 over 45 lines and 12 customers,
    against ฿83,171.09 that does land on a card.

    Deliberately the residual against the cards ACTUALLY RENDERED, not against
    the full aggregate: what the footnote promises is "this much is not in the
    list above", so it has to be computed from that list."""
    import price_lookup
    total = conn.execute(f"""
        SELECT COALESCE(SUM(s.net), 0) FROM sales_transactions s
        WHERE {where} AND {price_lookup.returned_lines_filter('s')}
    """, params).fetchone()[0]
    return round(total - sum(c['returned_net'] for c in cards), 2)


def product_rows(conn, where, params):
    """The aggregate half of `_customer_product_cards` (every (product, unit)
    with times_bought > 0), before the top-20 union and the price enrichment."""
    rows = [dict(r) for r in conn.execute(f"""
        SELECT s.product_id, COALESCE(p.product_name, s.product_name_raw) AS name,
               s.unit,
               COUNT(DISTINCT CASE WHEN {price_lookup.purchase_population_filter('s')}
                                   THEN s.doc_base END) AS times_bought,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.qty ELSE 0 END), 0) AS returned_qty,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.net ELSE 0 END), 0) AS returned_net,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                        THEN -s.qty ELSE s.qty END) AS total_qty,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                        THEN -s.net ELSE s.net END) AS total_net
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where} AND ({price_lookup.purchase_population_filter('s')}
                           OR {price_lookup.returned_lines_filter('s')})
        GROUP BY s.product_id, s.unit
        HAVING times_bought > 0
        ORDER BY s.product_id, s.unit
    """, params).fetchall()]
    return rows


def customers_list_rows(conn, search=None):
    """get_customers' billing_sql (verbatim), rows as dicts, no region/paging."""
    conds = []
    billing_params = []
    if search:
        conds.append(
            "(s.customer LIKE ? OR s.customer_code LIKE ? OR c.name LIKE ?)")
        billing_params += [f"%{search}%"] * 3
    conds.append(sales_filters.not_a_sale_clause('s'))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    billing_sql = f"""
        SELECT s.customer                                AS customer,
               s.customer_code                            AS customer_code,
               COALESCE(c.address, '')                    AS address,
               COALESCE(sp.name, c.salesperson)           AS salesperson,
               c.salesperson                              AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)                    AS salesperson_orphan,
               COUNT(DISTINCT s.doc_base)                 AS doc_count,
               COALESCE(SUM({sales_filters.purchase_net_sql('s')}), 0) AS total_net,
               MAX(s.date_iso)                            AS last_date,
               (c.code IS NULL)                           AS missing_master,
               (SELECT MAX(s2.date_iso) FROM sales_transactions s2
                 WHERE s2.customer_code IS s.customer_code
                   AND {price_lookup.purchase_population_filter('s2')}) AS last_purchase_date
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        {where}
        GROUP BY s.customer_code
    """
    return [dict(r) for r in conn.execute(billing_sql, billing_params).fetchall()]


def trip_last_sale(conn):
    """{customer code: last_sale} -- the sales-trip subquery, per master code."""
    return {r['code']: r['last_sale'] for r in conn.execute(f"""
        SELECT c.code,
               (SELECT MAX(date_iso) FROM sales_transactions s
                 WHERE s.customer_code = c.code
                   AND {price_lookup.purchase_population_filter('s')}) AS last_sale
          FROM customers c""").fetchall()}


def call_spend(conn, cutoff=None):
    """{canonical key: spend} -- get_call_list step 2, verbatim."""
    spend_params = []
    spend_where = ("WHERE customer NOT LIKE 'หน้าร้าน%' "
                   f"AND {sales_filters.not_a_sale_clause()}")
    if cutoff:
        spend_where += " AND date_iso >= ?"
        spend_params.append(cutoff)
    rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(customer_code),''), customer) AS canonical_code,
            SUM({sales_filters.purchase_net_sql()}) AS spend
        FROM sales_transactions
        {spend_where}
        GROUP BY canonical_code
    """, spend_params).fetchall()
    return {row['canonical_code']: row['spend'] or 0.0
            for row in rows if row['canonical_code']}


def call_last_buy(conn):
    """{canonical key: last_buy} -- get_call_list step 3, verbatim."""
    rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(customer_code),''), customer) AS canonical_code,
            MAX(date_iso) AS last_buy
        FROM sales_transactions
        WHERE {price_lookup.purchase_population_filter('')}
        GROUP BY canonical_code
    """).fetchall()
    return {row['canonical_code']: row['last_buy']
            for row in rows if row['canonical_code']}
