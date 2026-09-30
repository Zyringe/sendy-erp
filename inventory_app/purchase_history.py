"""A customer's purchase history: the facts CONTEXT.md "Customer page" defines,
keyed on the canonical customer key. One definition, imported by every surface
(card C, round-3 architecture review).

KEY. The customer code. A row with NO code (a credit note filed without one) takes
the one code that carries the same exact bill name (A1, Put 2026-09-30, read-time
only, nothing written); with no such code, or two, the key is the bill name (a true
orphan, never guessed): `customer_key_sql`. For every row that has a code it equals
`customer_code = ?` (0 padded, 0 blank on the 2026-09-29 PROD snapshot).

COST. `history()` filters on the key EXPRESSION, which no index serves, so each
call scans sales_transactions (19-28 ms on 20.6k rows, whatever the customer's
size) and grows with the table. One key per request is fine; any loop over
keys MUST use `histories()`, one pass for all of them. Upgrade path if a
single call ever matters: an expression index on `customer_key_sql('')`.

WINDOWS. `history(date_from, date_to)` bounds every field EXCEPT `winback`, which
is always all-time. `histories(total_since)` bounds `purchase_total` only. A
caller cannot get either wrong: the rule lives here, not at the call site.

DEFINITIONS (each is the CONTEXT.md term, one rule per line):
  ยอดซื้อรวม / monthly   SUM(sales_filters.purchase_net_sql) over the rows that are
                         not "invoiced in error" (sales_filters.not_a_sale_clause).
                         Credit notes subtract; the total can be negative.
  จำนวนชิ้น              same population, sales_filters.sales_qty_sql.
  จำนวนเอกสาร / ช่วงเวลา  raw over the same rows: a credit note IS a document.
  ครั้งที่ซื้อ / ซื้อล่าสุด / ซื้อครั้งแรก
                         price_lookup.purchase_population_filter, ONE query for the
                         count and the dates, so the newest document counted IS
                         the last purchase.
  per product            one row per (product, unit) with times_bought > 0: qty and
                         money net of returns, plus what was returned and the last
                         PAID purchase of that product.
  win-back               winback.compute_winback over the all-time scope.

This module answers history facts only. It never reads the price-evidence
predicate or the write-off table by name: price and AR keep their own
populations (`price_lookup`, `cashflow.BSN_AR_PREDICATE`), and consume the rows
returned here.

Plain dicts, like winback.py. No module-level caches (gunicorn -w 2).
"""
import document_kind
import sales_filters
import vat_math


# A bill name that maps to exactly ONE customer code (Put A1, 2026-09-30). Unaliased,
# uncorrelated, so SQLite evaluates it once per statement.
_ONE_CODE_NAMES = ("SELECT customer FROM sales_transactions "
                   "WHERE TRIM(COALESCE(customer_code,'')) != '' AND customer IS NOT NULL "
                   "GROUP BY customer HAVING COUNT(DISTINCT TRIM(customer_code)) = 1")


def customer_key_sql(alias='s'):
    """SQL expression of a sales row's canonical customer key: the trimmed
    customer code; for a row with NO code, the one code that carries the same exact
    bill name (a credit note filed without a code belongs to its shop, A1); else the
    bill name (a true orphan: no twin, or a name shared by two codes: never guessed).
    `alias` is required (the expression correlates on the row's bill name)."""
    if not alias:
        raise ValueError('customer_key_sql needs a table alias')
    a = alias
    return ("COALESCE(NULLIF(TRIM({a}.customer_code),''), "
            "(SELECT MIN(TRIM(k.customer_code)) FROM sales_transactions k "
            "WHERE k.customer = {a}.customer AND TRIM(COALESCE(k.customer_code,'')) != '' "
            "GROUP BY k.customer HAVING COUNT(DISTINCT TRIM(k.customer_code)) = 1), "
            "{a}.customer)").format(a=a)


def _key_match():
    """(sql, params-per-key) matching the rows of ONE key, on unaliased columns:
    the same answer as `customer_key_sql(..) = ?` without correlating on the outer
    alias, so it can sit in a WHERE whose FROM is aliased or not."""
    sql = ("(TRIM(COALESCE(customer_code,'')) = ? "
           "OR (TRIM(COALESCE(customer_code,'')) = '' AND ("
           "customer IN (SELECT customer FROM sales_transactions "
           "WHERE TRIM(COALESCE(customer_code,'')) != '' AND customer IS NOT NULL "
           "GROUP BY customer HAVING COUNT(DISTINCT TRIM(customer_code)) = 1 "
           "AND MIN(TRIM(customer_code)) = ?) "
           "OR (customer = ? AND customer NOT IN ({one}))))) ").format(one=_ONE_CODE_NAMES)
    return sql.rstrip(), 3


def _scope(key, date_from=None, date_to=None):
    """WHERE clause (unaliased columns) + params for one customer's counted rows:
    the key, the optional date window, and not-invoiced-in-error."""
    match, n = _key_match()
    conds = [match]
    params = [key] * n
    if date_from:
        conds.append('date_iso >= ?')
        params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?')
        params.append(date_to)
    conds.append(sales_filters.not_a_sale_clause())
    return ' AND '.join(conds), params


def customer_documents(conn, where, params, limit=None):
    """One row per DOCUMENT (doc_base), never per line — the shared grouping
    #493 introduced. `sales_transactions.doc_no` carries a per-line '-N'
    suffix, so grouping by it (the pre-#493 code) produced one "document" per
    LINE: 247 lines on customer 23ท06 read as 247 documents when the real
    count is 66. `history()`'s documents list and the mobile quick
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


def documents(conn, key, date_from=None, date_to=None, limit=None):
    """Just `history()['documents']` (newest first), optionally the first `limit`."""
    where, params = _scope(key, date_from, date_to)
    return customer_documents(conn, where, params, limit=limit)


def _totals(conn, where, params):
    """The eight `totals` fields for an already-built scope (two statements)."""
    import price_lookup

    raw = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS doc_count,
               COALESCE(SUM({sales_filters.purchase_net_sql()}), 0) AS purchase_total,
               COALESCE(SUM({sales_filters.sales_qty_sql()}), 0) AS qty_total,
               MIN(date_iso) AS first_activity,
               MAX(date_iso) AS last_activity
        FROM sales_transactions
        WHERE {where}
    """, params).fetchone()
    bought = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_base) AS purchase_count,
               MIN(date_iso)            AS first_purchase,
               MAX(date_iso)            AS last_purchase
        FROM sales_transactions
        WHERE {where} AND {price_lookup.purchase_population_filter('')}
    """, params).fetchone()
    return {**dict(raw), **dict(bought)}


def totals(conn, key, date_from=None, date_to=None):
    """Just `history()['totals']`: two statements instead of eight. For a surface
    that renders a count and a sum (/m/customer) and needs nothing else."""
    where, params = _scope(key, date_from, date_to)
    return _totals(conn, where, params)


def products(conn, key, date_from=None, date_to=None, counted=False, include_unbought=False):
    """Just `history()['products']`: one statement. For a surface that renders the
    per-product table and needs nothing else (the call card's rows).

    `counted=True` adds `qty_counted` / `net_counted` to every row: quantity and
    money over EVERY counted line of the (product, unit), net of credit notes
    (sales_filters.sales_qty_sql / sales_net_sql, the trade screens' figures), so a
    free (แถม) line and a line the purchase population drops still count. The call
    card's ซื้อรวม has always been that figure; `qty` / `net` are the customer
    page's stricter ones. Same statement, so a removal committed by an import can
    never leave the two figures describing different rows.

    `include_unbought=True` (only with `counted`) also returns the (product, unit)
    groups with `times_bought = 0` (only returned, given free or invoiced in error):
    the call card's clearance panel seeds from everything the customer received."""
    import price_lookup

    where, params = _scope(key, date_from, date_to)
    # Money and quantity are NET of credit notes (#646); times_bought and
    # last_purchase stay on the purchase half (a credit note is not a purchase,
    # the invoice it reverses still is). HAVING drops a product that was only
    # ever returned, given away or invoiced in error. Every CASE says ELSE 0, so
    # a row outside both halves adds nothing to qty / net (it does to *_counted).
    having = '' if (counted and include_unbought) else 'HAVING times_bought > 0'
    rows = [dict(r) for r in conn.execute(f"""
        SELECT s.product_id, COALESCE(p.product_name, s.product_name_raw) AS name,
               s.unit,
               COUNT(DISTINCT CASE WHEN {price_lookup.purchase_population_filter('s')}
                                   THEN s.doc_base END) AS times_bought,
               MAX(CASE WHEN {price_lookup.purchase_population_filter('s')}
                        THEN s.date_iso END) AS last_purchase,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')} THEN -s.qty
                        WHEN {price_lookup.purchase_population_filter('s')} THEN s.qty
                        ELSE 0 END) AS qty,
               SUM(CASE WHEN {price_lookup.returned_lines_filter('s')} THEN -s.net
                        WHEN {price_lookup.purchase_population_filter('s')} THEN s.net
                        ELSE 0 END) AS net,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.qty ELSE 0 END), 0) AS returned_qty,
               COALESCE(SUM(CASE WHEN {price_lookup.returned_lines_filter('s')}
                                 THEN s.net ELSE 0 END), 0) AS returned_net,
               SUM({sales_filters.sales_qty_sql('s')}) AS qty_counted,
               SUM({sales_filters.sales_net_sql('s')}) AS net_counted
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        GROUP BY s.product_id, s.unit
        {having}
        ORDER BY s.product_id, s.unit
    """, params).fetchall()]
    if not counted:
        for r in rows:
            del r['qty_counted'], r['net_counted']
    return rows


def history(conn, key, date_from=None, date_to=None, today=None):
    """Everything one customer page / call card needs. The window applies to
    every field EXCEPT 'winback' (always all-time). `today` pins win-back's clock.

      {'key': key,
       'totals': {'purchase_total', 'qty_total', 'doc_count', 'first_activity',
                  'last_activity', 'purchase_count', 'first_purchase',
                  'last_purchase'},
       'monthly':   [{'month', 'doc_count', 'total_net'}],
       'documents': [...],       # one row per document, newest first
       'top_products': [...],    # money-ordered net of returns, top 20 (แบรนด์เด่น)
       'products': [{'product_id', 'name', 'unit', 'times_bought',
                     'last_purchase', 'qty', 'net', 'returned_qty',
                     'returned_net'}],   # every (product, unit) bought, ORDER BY
                                         # product_id, unit
       'returned_net_total': float,      # all countable credit notes in the window
       'winback': [...]}                 # all-time
    """
    import price_lookup
    import winback

    where, params = _scope(key, date_from, date_to)

    totals = _totals(conn, where, params)

    monthly = [dict(r) for r in conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_base) AS doc_count,
               SUM({sales_filters.purchase_net_sql()}) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()]

    documents = customer_documents(conn, where, params)

    # A MAPPED line groups on product_id alone (a credit note prints
    # product_name_raw differently from the invoice it reverses); an UNMAPPED
    # line keeps its raw name.
    top_products = [dict(r) for r in conn.execute(f"""
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
    """, params).fetchall()]

    products_ = products(conn, key, date_from, date_to)

    returned_net_total = conn.execute(f"""
        SELECT COALESCE(SUM(s.net), 0) FROM sales_transactions s
        WHERE {where} AND {price_lookup.returned_lines_filter('s')}
    """, params).fetchone()[0]

    wb_where, wb_params = _scope(key)
    winback_rows = winback.compute_winback(conn, wb_where, wb_params, today=today)

    return {
        'key': key,
        'totals': totals,
        'monthly': monthly,
        'documents': documents,
        'top_products': top_products,
        'products': products_,
        'returned_net_total': returned_net_total,
        'winback': winback_rows,
    }


def histories(conn, total_since=None, with_bill_name=False):
    """{key: {'purchase_total', 'doc_count', 'last_activity', 'last_purchase',
    'bill_name'}} for every key with at least one sales row. `purchase_total`
    honours `total_since` (ISO date, inclusive); every other field is all-time.
    `bill_name` is the name on the key's newest row, but only when
    `with_bill_name` is set: it costs a second full scan (~12 ms), and only
    /customers reads it; otherwise it is None.

    Universe is every key, marketplace `หน้าร้าน` accounts included: a caller
    that must not list them filters the keys itself. A key whose every row is
    "invoiced in error" is still present, with zeros.
    """
    import price_lookup

    key = customer_key_sql('s')
    since_sql = 'AND s.date_iso >= ?' if total_since else ''
    counted = sales_filters.not_a_sale_clause('s')
    rows = conn.execute(f"""
        SELECT {key} AS k,
               COALESCE(SUM(CASE WHEN {counted} {since_sql}
                                 THEN {sales_filters.purchase_net_sql('s')} END), 0)
                   AS purchase_total,
               COUNT(DISTINCT CASE WHEN {counted} THEN s.doc_base END) AS doc_count,
               MAX(CASE WHEN {counted} THEN s.date_iso END) AS last_activity,
               MAX(CASE WHEN {price_lookup.purchase_population_filter('s')}
                        THEN s.date_iso END) AS last_purchase
        FROM sales_transactions s
        WHERE {key} IS NOT NULL
        GROUP BY k
    """, [total_since] if total_since else []).fetchall()
    out = {r['k']: {'purchase_total': r['purchase_total'],
                    'doc_count': r['doc_count'],
                    'last_activity': r['last_activity'],
                    'last_purchase': r['last_purchase'],
                    'bill_name': None}
           for r in rows}

    if not with_bill_name:
        return out

    # Newest row wins; ties on the date fall to the higher id.
    # A key can commit between the two reads (separate snapshots): skip it, the
    # next call sees it whole.
    for r in conn.execute(f"""
        SELECT {key} AS k, s.customer
        FROM sales_transactions s
        WHERE {key} IS NOT NULL AND s.customer IS NOT NULL
        ORDER BY s.date_iso, s.id
    """):
        if r['k'] in out:
            out[r['k']]['bill_name'] = r['customer']
    return out
