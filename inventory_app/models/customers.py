"""Customers + regions + BSN customer-master import + geocode — extracted
verbatim from models.py (behavior-preserving split, Phase 11) — see
models/__init__.py's module docstring for the overall file-split
rationale. No behavior changes.
"""
import json
import sales_filters
from database import get_connection


def _customer_sales_scope(key_col, key_value, date_from, date_to):
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


def _customer_sales_aggregates(conn, where, params):
    """The four per-customer sales aggregates, defined ONCE.

    The name-keyed and code-keyed summaries below differ only in their WHERE, and
    two copies of these queries drift invisibly — the same reason `sales_filters`
    and `commission_attribution` exist as single definitions.

    Returns (summary, top_products, monthly, docs).
    """
    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM sales_transactions
        WHERE {where}
    """, params).fetchone()

    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, s.product_name_raw) AS name,
               p.id AS product_id,
               s.unit,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net,
               COUNT(DISTINCT s.doc_no) AS doc_count
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        GROUP BY s.product_id, s.product_name_raw
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    # All invoices (paginated not needed here — keep it simple, limit 200)
    docs = conn.execute(f"""
        SELECT date_iso, doc_no,
               COUNT(*) AS line_count,
               SUM(qty) AS total_qty,
               SUM(net) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY doc_no
        ORDER BY date_iso DESC, doc_no
        LIMIT 200
    """, params).fetchall()

    return summary, top_products, monthly, docs


def get_customer_summary(customer, date_from=None, date_to=None):
    """
    Returns summary + top products + monthly trend for a specific customer.

    Keyed on the BILL name. One bill name can span >1 physical company (BUG 2 —
    ทรัพย์ทวี), so this merges them; `get_customer_summary_by_code` is the
    unambiguous form and is what the customer page uses. This one survives for
    `call_card.py`, which only ever has a name.
    """
    conn = get_connection()
    where, params = _customer_sales_scope('customer', customer, date_from, date_to)
    summary, top_products, monthly, docs = _customer_sales_aggregates(
        conn, where, params)

    # Pull region + salesperson from customers MASTER (post-D1 view migration).
    # 3-way fallback: salespersons.name → customers.salesperson code → '(ไม่กำหนด)'.
    # Same for region: regions.name_th → regions.code → '(ไม่ระบุ)'.
    master_row = conn.execute("""
        SELECT s.customer_code,
               c.code AS master_code, c.name AS master_name,
               c.salesperson AS sp_code, c.region_id,
               sp.name AS sp_name, sp.is_active AS sp_active,
               r.code AS region_code, r.name_th AS region_name
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        LEFT JOIN regions       r  ON r.id    = c.region_id
        WHERE s.customer = ?
        LIMIT 1
    """, [customer]).fetchone()

    customer_info = None
    customer_code = None
    salesperson_code = None
    salesperson_display = None
    salesperson_orphan = False
    region_code = None
    region_display = None

    if master_row:
        customer_code = master_row['customer_code']
        if master_row['master_code']:
            row = conn.execute(
                "SELECT * FROM customers WHERE code=?", [master_row['master_code']]
            ).fetchone()
            if row:
                customer_info = dict(row)
            salesperson_code = master_row['sp_code']
            if salesperson_code:
                if master_row['sp_name']:
                    salesperson_display = master_row['sp_name']
                else:
                    salesperson_display = salesperson_code
                    salesperson_orphan = True
            region_code = master_row['region_code']
            region_display = master_row['region_name'] or master_row['region_code']

    conn.close()
    return {
        'customer': customer,
        'customer_code': customer_code,
        'region': region_display,
        'region_code': region_code,
        'salesperson': salesperson_display,
        'salesperson_code': salesperson_code,
        'salesperson_orphan': salesperson_orphan,
        'customer_info': customer_info,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


def resolve_customer_codes(name):
    """Bill names can span >1 physical company (BUG 2, 2026-08 grilling —
    e.g. 'ทรัพย์ทวี' is both 43ท013 'ร้าน ทรัพย์ทวี' and 01พ14 'บจก. พงศ์ทรัพย์ทวี').
    Callers must never silently pick one — this just reports what's there.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT customer_code
        FROM sales_transactions
        WHERE customer = ? AND customer_code IS NOT NULL
        ORDER BY customer_code
    """, [name]).fetchall()
    conn.close()
    return [r['customer_code'] for r in rows]


def get_customer_summary_by_code(customer_code, date_from=None, date_to=None):
    """Code-keyed counterpart to get_customer_summary().

    Unlike get_customer_summary (keyed on the bill name in sales_transactions),
    this resolves the master row DIRECTLY from `customers` by code, so the
    2,390 customers with no sales_transactions rows still render, and the two
    companies that share a bill name (BUG 2) never merge into one page.
    Returns the same dict shape; `data['customer']` is the bill name when one
    exists for this code, else the master name.
    """
    conn = get_connection()
    where, params = _customer_sales_scope(
        'customer_code', customer_code, date_from, date_to)
    summary, top_products, monthly, docs = _customer_sales_aggregates(
        conn, where, params)

    # Bill (short) name for THIS code specifically — most recent sale wins.
    # This is what distinguishes ทรัพย์ทวี's two codes; a name-keyed lookup
    # cannot (BUG 2).
    bill_name_row = conn.execute("""
        SELECT customer FROM sales_transactions
        WHERE customer_code = ? AND customer IS NOT NULL
        ORDER BY date_iso DESC LIMIT 1
    """, [customer_code]).fetchone()
    bill_name = bill_name_row['customer'] if bill_name_row else None

    # Master row is the anchor — resolves even when this code has zero sales.
    master_row = conn.execute("""
        SELECT c.code AS master_code, c.name AS master_name,
               c.salesperson AS sp_code, c.region_id,
               sp.name AS sp_name, sp.is_active AS sp_active,
               r.code AS region_code, r.name_th AS region_name
        FROM customers c
        LEFT JOIN salespersons sp ON sp.code = c.salesperson
        LEFT JOIN regions r ON r.id = c.region_id
        WHERE c.code = ?
    """, [customer_code]).fetchone()

    customer_info = None
    salesperson_code = None
    salesperson_display = None
    salesperson_orphan = False
    region_code = None
    region_display = None
    display_name = bill_name or customer_code

    if master_row:
        row = conn.execute(
            "SELECT * FROM customers WHERE code=?", [customer_code]
        ).fetchone()
        if row:
            customer_info = dict(row)
        if not bill_name:
            display_name = master_row['master_name']
        salesperson_code = master_row['sp_code']
        if salesperson_code:
            if master_row['sp_name']:
                salesperson_display = master_row['sp_name']
            else:
                salesperson_display = salesperson_code
                salesperson_orphan = True
        region_code = master_row['region_code']
        region_display = master_row['region_name'] or master_row['region_code']

    conn.close()
    return {
        # Date-INDEPENDENT: both source queries above ignore date_from/date_to, so
        # a customer whose filter excludes every bill still reads exists=True. The
        # route 404s on False; keying that off the filtered rows would 404 real
        # customers mid-filter.
        'exists': bool(master_row or bill_name_row),
        'customer': display_name,
        'customer_code': customer_code,
        'region': region_display,
        'region_code': region_code,
        'salesperson': salesperson_display,
        'salesperson_code': salesperson_code,
        'salesperson_orphan': salesperson_orphan,
        'customer_info': customer_info,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


def get_regions():
    """Region list for filter dropdowns. Returns [{id, code, name_th}].
    Driven by the regions master (migration 010), not the legacy
    customer_regions snapshot."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, code, name_th FROM regions ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customers(search=None, region=None, region_id=None, page=1, per_page=50,
                   include_billless=False):
    """Customer list backed by customers master + salespersons + regions.

    Filter precedence: region_id (FK, new) > region (text, legacy URL).
    Returns customer rows with display fields:
        salesperson  → name from salespersons master, or raw code if orphan
        region       → name_th from regions, or code as fallback

    `include_billless=True` unions in `customers` master rows with NO
    sales_transactions row at all (doc_count 0, total_net 0, last_date NULL)
    — 2,390 of 2,665 customers, invisible here otherwise. Default False keeps
    today's billing-only, 275-row view unchanged.
    """
    conn = get_connection()
    conds = []
    billing_params = []
    if search:
        conds.append("(s.customer LIKE ? OR s.customer_code LIKE ?)")
        billing_params += [f"%{search}%", f"%{search}%"]

    rid_int = None
    if region_id is not None and str(region_id).strip():
        try:
            rid_int = int(region_id)
        except (ValueError, TypeError):
            rid_int = None
    elif region:
        # Legacy URL: ?region=<code or name_th>. Resolve to id.
        match = conn.execute(
            "SELECT id FROM regions WHERE code = ? OR name_th = ? LIMIT 1",
            (region, region),
        ).fetchone()
        if match:
            rid_int = match['id']
    if rid_int is not None:
        conds.append("c.region_id = ?")
        billing_params.append(rid_int)

    # Same exclusion as the customer DETAIL page — without it the list and the
    # detail disagree by the giveaway (proved: วรสวัสดิ์ ฿499,577.31 vs ฿345,454.51).
    conds.append(sales_filters.not_a_sale_clause('s'))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    billing_sql = f"""
        SELECT s.customer                                AS customer,
               s.customer_code                            AS customer_code,
               COALESCE(r.name_th, r.code)                AS region,
               r.code                                     AS region_code,
               c.region_id                                AS region_id,
               COALESCE(sp.name, c.salesperson)           AS salesperson,
               c.salesperson                              AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)                    AS salesperson_orphan,
               COUNT(DISTINCT s.doc_no)                   AS doc_count,
               COALESCE(SUM(s.net), 0)                    AS total_net,
               MAX(s.date_iso)                            AS last_date,
               (c.code IS NULL)                           AS missing_master
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        LEFT JOIN regions       r  ON r.id    = c.region_id
        {where}
        GROUP BY s.customer_code
    """
    union_parts = [billing_sql]
    params = list(billing_params)
    bl_params = []

    if include_billless:
        bl_conds = ["NOT EXISTS (SELECT 1 FROM sales_transactions s2 "
                    "WHERE s2.customer_code = c.code)"]
        if search:
            bl_conds.append("(c.name LIKE ? OR c.code LIKE ?)")
            bl_params += [f"%{search}%", f"%{search}%"]
        if rid_int is not None:
            bl_conds.append("c.region_id = ?")
            bl_params.append(rid_int)
        bl_where = "WHERE " + " AND ".join(bl_conds)

        billless_sql = f"""
            SELECT c.name                                 AS customer,
                   c.code                                  AS customer_code,
                   COALESCE(r.name_th, r.code)              AS region,
                   r.code                                   AS region_code,
                   c.region_id                              AS region_id,
                   COALESCE(sp.name, c.salesperson)         AS salesperson,
                   c.salesperson                            AS salesperson_code,
                   (c.salesperson IS NOT NULL
                      AND c.salesperson != ''
                      AND sp.code IS NULL)                  AS salesperson_orphan,
                   0                                         AS doc_count,
                   0                                         AS total_net,
                   NULL                                      AS last_date,
                   0                                         AS missing_master
            FROM customers c
            LEFT JOIN salespersons sp ON sp.code = c.salesperson
            LEFT JOIN regions      r  ON r.id    = c.region_id
            {bl_where}
        """
        union_parts.append(billless_sql)
        params = params + bl_params

    union_sql = "\nUNION ALL\n".join(union_parts)

    sql = f"""
        SELECT * FROM ({union_sql})
        ORDER BY customer
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    # `total` counts the ROWS this function can paginate, so it must be built
    # from the same GROUP BY the row query uses — not `COUNT(DISTINCT
    # s.customer_code)`, which the pre-Phase-3 code used and which SQL makes
    # skip NULL. ~21 sales_transactions rows carry a blank customer_code and
    # GROUP BY collapses them into one real, rendered row, so the old count was
    # one short of what the page shows (275 vs 276 today). That is a lie the
    # pagination maths is built on: the moment the undercount lands on a
    # multiple of per_page, the last row becomes unreachable. Fixing it moves
    # the displayed default figure 275 -> 276, which is the number of rows the
    # list actually has.
    billing_total = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT 1
            FROM sales_transactions s
            LEFT JOIN customers c ON c.code = s.customer_code
            {where}
            GROUP BY s.customer_code
        )
    """, billing_params).fetchone()[0]

    total = billing_total
    if include_billless:
        billless_total = conn.execute(f"""
            SELECT COUNT(*) FROM customers c {bl_where}
        """, bl_params).fetchone()[0]
        total += billless_total

    conn.close()
    return [dict(r) for r in rows], total


# ── Customer Assignment (salesperson + region on customers master) ────────────
# Migration 010 introduced customers.salesperson (TEXT code) + customers.region_id
# (FK regions.id). The legacy customer_regions table is the *display* source
# (read by get_customer_summary / get_customers above) until UI migration D1
# lands. The helpers below write to the MASTER table only — audit triggers on
# customers cover the change automatically.

def get_all_regions_with_counts():
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.id, r.code, r.name_th, r.sort_order, r.note,
               COUNT(c.code) AS customer_count
          FROM regions r
          LEFT JOIN customers c ON c.region_id = r.id
         GROUP BY r.id
         ORDER BY r.sort_order, r.code
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_region(region_id, name_th, sort_order, note):
    name_th = (name_th or '').strip() or None
    note    = (note or '').strip() or None
    try:
        sort_order = int(sort_order) if str(sort_order).strip() else 100
    except (ValueError, TypeError):
        return {'ok': False, 'error': 'sort_order ต้องเป็นจำนวนเต็ม'}

    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE regions SET name_th = ?, sort_order = ?, note = ? WHERE id = ?",
            (name_th, sort_order, note, region_id),
        )
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบ region id {region_id}'}
        conn.commit()
        return {'ok': True, 'error': None}
    finally:
        conn.close()


def get_active_salespersons():
    conn = get_connection()
    rows = conn.execute(
        "SELECT code, name FROM salespersons WHERE is_active = 1 ORDER BY code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_regions():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, code, name_th FROM regions ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_orphan_salesperson_codes():
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT salesperson AS code
        FROM customers
        WHERE salesperson IS NOT NULL
          AND salesperson != ''
          AND salesperson NOT IN (SELECT code FROM salespersons)
    """).fetchall()
    conn.close()
    return {r['code'] for r in rows}


def get_customer_master(customer_code):
    conn = get_connection()
    row = conn.execute(
        "SELECT code, name, salesperson, region_id FROM customers WHERE code = ?",
        [customer_code],
    ).fetchone()
    conn.close()
    return dict(row) if row else None


_BULK_MAX = 5000  # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; cap well below.


def update_customer_assignment(customer_code, salesperson_code, region_id):
    sp = (salesperson_code or '').strip() or None
    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is not None:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'region_id ไม่ถูกต้อง'}

    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT salesperson FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()
        if current is None:
            return {'ok': False, 'error': f'ไม่พบ customer code "{customer_code}"'}

        # Skip the active-salesperson check when the value is unchanged so a
        # customer with a legacy/orphan code can re-save other fields without
        # being forced to switch salesperson.
        if sp is not None and sp != current['salesperson']:
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}
        if rid is not None:
            if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
                return {'ok': False, 'error': f'ไม่พบ region id {rid}'}

        conn.execute(
            "UPDATE customers SET salesperson = ?, region_id = ? WHERE code = ?",
            (sp, rid, customer_code),
        )
        conn.commit()
        return {'ok': True, 'error': None}
    finally:
        conn.close()


# Group 2 (customer_summary.html plan.md terminology): the contact fields the
# customer-edit modal writes, alongside group 1 (salesperson/region_id above).
# NOT `name` (locked — see plan.md decision 3) and NOT the group-3 operational
# columns (customer_type/credit_days/tax_id/zone), which import_customers_from_bsn
# always overwrites even on a protected row — a form for those would lie.
CUSTOMER_CONTACT_FIELDS = ('nickname', 'phone', 'fax', 'contact', 'address', 'contact_note')

# customers column -> its customer_contact_review.proposed_* twin. Note the last
# pair is NOT a mechanical `proposed_` + name: the review column is
# `proposed_note`, not `proposed_contact_note`.
_REVIEW_COL = {
    'nickname':     'proposed_nickname',
    'phone':        'proposed_phone',
    'fax':          'proposed_fax',
    'contact':      'proposed_contact',
    'address':      'proposed_address',
    'contact_note': 'proposed_note',
}


def update_customer_edit(customer_code, salesperson_code, region_id, contact, username):
    """Customer-edit modal's save path: group 1 (salesperson/region_id) +
    group 2 (contact fields), one UPDATE per save.

    `contact` is a dict with `CUSTOMER_CONTACT_FIELDS` keys, raw form strings (blank
    means "clear this field" — the modal always echoes the live value back,
    so blank only happens when the field was already blank or Put cleared it
    on purpose).

    `contact_normalized_at`/`_by` are stamped ONLY when a contact field
    actually changed vs the live row — that stamp is what protects the row
    from the next BSN import overwriting it (import_customers_from_bsn checks
    `contact_normalized_at IS NOT NULL`). Stamping on every save, including a
    salesperson-only edit, would over-protect rows nobody touched contact on.

    When a contact field changed and a `customer_contact_review` row exists
    with status='pending', its proposed_* columns are updated to match —
    otherwise a later click on ยืนยัน in that queue silently reverts this
    edit (that page prefills from the frozen proposed_* snapshot, not the
    live row; 17 billing customers are in that state as of 2026-08-01).
    """
    sp = (salesperson_code or '').strip() or None
    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is not None:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'region_id ไม่ถูกต้อง'}

    # A short payload is a caller bug, never "clear the rest". The route already
    # branches on this, but this function is exported through the models facade,
    # so a future caller that skips the route would otherwise silently wipe the
    # keys it forgot — the exact failure this whole path was hardened against.
    # Re-checking here is a data-mutation invariant, not defensive decoration:
    # `contact[k]` below then cannot raise, and cannot default to blank.
    missing = [k for k in CUSTOMER_CONTACT_FIELDS if k not in contact]
    if missing:
        return {'ok': False,
                'error': f'contact payload ไม่ครบ (ขาด: {", ".join(missing)})'}

    new_contact = {k: (contact[k] or '').strip() or None for k in CUSTOMER_CONTACT_FIELDS}

    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT * FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()
        if current is None:
            return {'ok': False, 'error': f'ไม่พบ customer code "{customer_code}"'}

        if sp is not None and sp != current['salesperson']:
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}
        if rid is not None:
            if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
                return {'ok': False, 'error': f'ไม่พบ region id {rid}'}

        changed_fields = [k for k in CUSTOMER_CONTACT_FIELDS if new_contact[k] != current[k]]
        contact_changed = bool(changed_fields)

        if contact_changed:
            # Freeze the pre-edit values ONCE, so the state before the first
            # manual edit is always recoverable even if audit_log is ever
            # pruned. COALESCE means an existing snapshot is never clobbered.
            # ⚠ This is "before the first MANUAL edit", not "as Express sent
            # it" — if the normalizer already rewrote the row, its output is
            # what gets frozen. Same 4-key shape the import and the review
            # page write, so any reader sees one format. 1,674 of 2,665 rows
            # have no snapshot at all today.
            orig_json = json.dumps({
                'name':    current['name'],
                'phone':   current['phone'] or '',
                'contact': current['contact'] or '',
                'address': current['address'] or '',
            }, ensure_ascii=False)
            conn.execute("""
                UPDATE customers
                   SET salesperson = ?, region_id = ?,
                       nickname = ?, phone = ?, fax = ?, contact = ?,
                       address = ?, contact_note = ?,
                       contact_orig_json = COALESCE(contact_orig_json, ?),
                       contact_normalized_at = datetime('now','localtime'),
                       contact_normalized_by = ?
                 WHERE code = ?
            """, (sp, rid, *[new_contact[k] for k in CUSTOMER_CONTACT_FIELDS],
                  orig_json, username, customer_code))
        else:
            conn.execute(
                "UPDATE customers SET salesperson = ?, region_id = ? WHERE code = ?",
                (sp, rid, customer_code),
            )

        if contact_changed:
            pending = conn.execute("""
                SELECT id FROM customer_contact_review
                 WHERE customer_code = ? AND status = 'pending'
            """, (customer_code,)).fetchone()
            if pending:
                # ONLY the fields that actually changed. Writing all six would
                # destroy the normalizer's un-reviewed proposals for the fields
                # this edit never touched — 53 of the 62 pending rows have at
                # least one proposed_* that differs from the live value
                # (measured 2026-08-01), e.g. 11ม06's proposed_address carries a
                # postcode the live row lacks. Editing only the phone must not
                # silently drop that.
                sets = ', '.join(f'{_REVIEW_COL[k]} = ?' for k in changed_fields)
                conn.execute(
                    f"UPDATE customer_contact_review SET {sets} WHERE id = ?",
                    [new_contact[k] for k in changed_fields] + [pending['id']])

        conn.commit()
        return {'ok': True, 'error': None, 'contact_changed': contact_changed}
    finally:
        conn.close()


_AUDIT_FIELD_LABEL = {
    'name': 'ชื่อในทะเบียน', 'nickname': 'ชื่อเล่น', 'salesperson': 'เซลส์',
    'region_id': 'เขตการขาย', 'zone': 'โซน', 'address': 'ที่อยู่',
    'phone': 'โทรศัพท์', 'fax': 'แฟกซ์', 'contact': 'ผู้ติดต่อ',
    'contact_note': 'หมายเหตุ', 'tax_id': 'Tax ID', 'credit_days': 'เครดิต (วัน)',
    'lat': 'พิกัด lat', 'lng': 'พิกัด lng',
}


def get_customer_audit_history(customer_code, limit=15):
    """Recent changes to this customer's master row, newest first.

    `audit_customers_update` records `{field: [old, new]}` for every change
    (2,050 UPDATE rows as of 2026-08-01) — no page in the app read it, so the
    trail was only reachable by opening SQL. This makes it visible where the
    edits happen.

    Joined on `audit_log.row_key` (migration 150), NOT `customers.rowid`.
    `row_id` stores the SQLite rowid, which is IMPLICIT for this table (PK
    is TEXT `code`) — VACUUM is explicitly permitted to renumber an implicit
    rowid, and a renumber would re-point old rows at whatever customer now
    holds that rowid, confidently showing ANOTHER customer's history. That
    is why the card was pulled from #346 rather than shipped with a comment.
    `row_key` stores the business key (`customers.code`) directly at write
    time, so this query needs no join back through `customers` at all — and
    migration 151 both made `code` itself immutable and added the
    `(table_name, row_key)` index this query uses.

    ⚠ `audit_log.user` is NULL on every customers row (1,188 INSERT + 2,050
    UPDATE, checked 2026-08-01): the trigger is SQL-level and cannot see the
    logged-in user. So this answers "when + what", never "who" — the
    template must not imply otherwise.

    Returns [{created_at, action, changes: [{field, label, old, new}]}].
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT a.created_at, a.action, a.changed_fields
          FROM audit_log a
         WHERE a.table_name = 'customers'
           AND a.row_key = ?
         ORDER BY a.id DESC
         LIMIT ?
    """, (customer_code, limit)).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            parsed = json.loads(r['changed_fields'] or '{}')
        except (ValueError, TypeError):
            parsed = {}
        changes = []
        for field, val in parsed.items():
            # UPDATE rows carry [old, new]; INSERT rows carry a bare value.
            if isinstance(val, list) and len(val) == 2:
                old, new = val
            else:
                old, new = None, val
            changes.append({
                'field': field,
                'label': _AUDIT_FIELD_LABEL.get(field, field),
                'old': '' if old is None else old,
                'new': '' if new is None else new,
            })
        out.append({'created_at': r['created_at'], 'action': r['action'],
                    'changes': changes})
    return out


def bulk_reassign_customers(customer_codes, region_id, salesperson_code=None):
    """Region-only bulk reassignment of the customers master.

    Put's decision (plan Phase 3): no UI may move `customers.salesperson` for
    many customers in one click — commission rules (models/commission.py) do
    NOT follow a master-record salesperson change, and 472 customers carry an
    active commission rule perfectly aligned with their master today. A bulk
    click that silently drifted even a few of those would be very hard to
    notice. A request that still carries a salesperson target (e.g. a stale
    page rendered before this deploy) is REJECTED, not silently dropped —
    same "missing/extra is not clear, ask again" spirit as Phase 2's
    customer_reassign.
    """
    if (salesperson_code or '').strip():
        return {'ok': False, 'updated': 0,
                'error': 'เปลี่ยน salesperson แบบกลุ่มถูกปิดแล้ว — คอมมิชชั่นไม่ขยับตาม '
                         'master record; แก้ทีละรายที่หน้าลูกค้า หรือใช้ /commission/reassign'}
    if not customer_codes:
        return {'ok': False, 'updated': 0, 'error': 'ไม่มีลูกค้าที่เลือก'}
    if len(customer_codes) > _BULK_MAX:
        return {'ok': False, 'updated': 0,
                'error': f'เลือกได้สูงสุด {_BULK_MAX} ลูกค้า (เลือก {len(customer_codes)})'}

    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is None:
        return {'ok': False, 'updated': 0, 'error': 'กรุณาเลือก region ปลายทาง'}
    try:
        rid = int(rid)
    except (ValueError, TypeError):
        return {'ok': False, 'updated': 0, 'error': 'region_id ไม่ถูกต้อง'}

    conn = get_connection()
    try:
        if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
            return {'ok': False, 'updated': 0, 'error': f'ไม่พบ region id {rid}'}

        placeholders = ','.join(['?'] * len(customer_codes))
        sql = f"UPDATE customers SET region_id = ? WHERE code IN ({placeholders})"
        params = [rid, *customer_codes]

        with conn:
            cur = conn.execute(sql, params)
        return {'ok': True, 'updated': cur.rowcount, 'error': None}
    finally:
        conn.close()


def get_customers_master(search=None, salesperson=None, region_id=None,
                         orphan_only=False, page=1, per_page=100):
    conn = get_connection()
    conds = []
    params = []
    if search:
        conds.append("(c.code LIKE ? OR c.name LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if salesperson == '__none__':
        conds.append("(c.salesperson IS NULL OR c.salesperson = '')")
    elif salesperson:
        conds.append("c.salesperson = ?")
        params.append(salesperson)
    if region_id:
        conds.append("c.region_id = ?")
        params.append(int(region_id))
    if orphan_only:
        conds.append(
            "c.salesperson IS NOT NULL AND c.salesperson != '' "
            "AND c.salesperson NOT IN (SELECT code FROM salespersons)"
        )
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT c.code, c.name, c.salesperson AS salesperson_code,
               s.name AS salesperson_name, s.is_active AS salesperson_active,
               c.region_id, r.code AS region_code, r.name_th AS region_name
        FROM customers c
        LEFT JOIN salespersons s ON s.code = c.salesperson
        LEFT JOIN regions      r ON r.id   = c.region_id
        {where}
        ORDER BY c.name
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM customers c {where}", params
    ).fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


def import_customers_from_bsn(customers):
    """Import BSN customer master rows with contact-protection and auto-sanitization.

    Returns (inserted, updated, protected):
      - inserted: new rows created
      - updated:  existing un-normalized rows refreshed (contact fields may be sanitized)
      - protected: existing rows with contact_normalized_at IS NOT NULL — only
                   operational fields (salesperson, zone, customer_type, credit_days,
                   tax_id, imported_at) are updated; all contact fields are preserved.
    """
    from customer_contact_normalize import normalize_customer

    conn = get_connection()
    inserted = updated = protected = 0

    # Commission reassignments override the source file's owner.
    #
    # This import refreshes `customers.salesperson` from the file on every
    # path, and Express still lists departed reps as the owner — น้อย /02 is on
    # 168 customers there. Without this, one import silently undoes migrations
    # 143/144 and every rule created through /commission/reassign.
    #
    # Same failure shape the reassignment table exists to solve:
    # `received_payments.salesperson` is UPSERT-ed by the weekly import, which
    # is why the decision lives in its own table rather than being edited into
    # the imported row. The rule is a decision made AFTER the file was
    # produced, so it wins; name/address/contact still refresh normally.
    #
    # Applied BEFORE the write, not corrected after: writing the file's value
    # and fixing it afterwards lands the right data but fires the
    # audit_customers_update trigger twice, recording a change that never
    # happened. The first cut of this guard did exactly that and put 936
    # phantom rows (468 out, 468 back) into a 2,718-row import — 34% noise in
    # the trail people rely on to answer "who changed this customer".
    reassigned = {r['customer_code']: r['to_salesperson'] for r in conn.execute("""
        SELECT customer_code, to_salesperson FROM commission_customer_reassign r
         WHERE is_active = 1
           AND effective_from = (SELECT MAX(r2.effective_from)
                                   FROM commission_customer_reassign r2
                                  WHERE r2.customer_code = r.customer_code
                                    AND r2.is_active = 1)
    """)}

    for c in customers:
        if c['code'] in reassigned:
            c = dict(c)
            c['salesperson'] = reassigned[c['code']]
        existing = conn.execute(
            "SELECT code, contact_normalized_at FROM customers WHERE code=?",
            (c['code'],)
        ).fetchone()

        if existing and existing['contact_normalized_at'] is not None:
            # ── PROTECTED branch: row has been cleaned — touch only operational cols ──
            conn.execute("""
                UPDATE customers
                   SET salesperson=?, zone=?, customer_type=?,
                       credit_days=?, tax_id=?,
                       imported_at=datetime('now','localtime')
                 WHERE code=?
            """, (c['salesperson'], c['zone'], c['customer_type'],
                  c['credit_days'], c['tax_id'], c['code']))
            protected += 1

        else:
            # ── UN-NORMALIZED / NEW branch: sanitize via normalizer ──
            res = normalize_customer({
                'name':    c['name'],
                'phone':   c.get('phone') or '',
                'contact': c.get('contact') or '',
                'address': c.get('address') or '',
            })

            prop = res['proposed']
            imp_phone   = c.get('phone') or ''
            imp_fax     = ''
            imp_contact = c.get('contact') or ''

            # Determine whether the normalizer found a meaningful, lossless change
            auto_changed = (
                res['confidence'] == 'auto'
                and (
                    prop['phone'] != imp_phone
                    or prop['fax']   # non-empty fax extracted
                    or prop['contact'] != imp_contact
                )
            )

            if auto_changed:
                out_phone   = prop['phone'] or None
                out_fax     = prop['fax'] or None
                out_contact = prop['contact'] or None
                out_note    = prop.get('note') or None
                orig_json   = json.dumps({
                    'name':    c['name'],
                    'phone':   imp_phone,
                    'contact': imp_contact,
                    'address': c.get('address') or '',
                }, ensure_ascii=False)
                normalized_at  = "datetime('now','localtime')"
                normalized_by  = 'bsn_import'
            else:
                out_phone   = imp_phone or None
                out_fax     = None
                out_contact = imp_contact or None
                orig_json   = None
                normalized_at  = None
                normalized_by  = None

            if existing:
                if auto_changed:
                    conn.execute("""
                        UPDATE customers
                           SET name=?, salesperson=?, zone=?, customer_type=?,
                               address=?, phone=?, fax=?, tax_id=?, credit_days=?,
                               contact=?, contact_note=?, contact_orig_json=?,
                               contact_normalized_at=datetime('now','localtime'),
                               contact_normalized_by=?,
                               imported_at=datetime('now','localtime')
                         WHERE code=?
                    """, (c['name'], c['salesperson'], c['zone'], c['customer_type'],
                          c.get('address'), out_phone, out_fax,
                          c['tax_id'], c['credit_days'],
                          out_contact, out_note, orig_json, normalized_by, c['code']))
                else:
                    conn.execute("""
                        UPDATE customers
                           SET name=?, salesperson=?, zone=?, customer_type=?,
                               address=?, phone=?, tax_id=?, credit_days=?,
                               contact=?, imported_at=datetime('now','localtime')
                         WHERE code=?
                    """, (c['name'], c['salesperson'], c['zone'], c['customer_type'],
                          c.get('address'), out_phone, c['tax_id'], c['credit_days'],
                          out_contact, c['code']))
                updated += 1
            else:
                if auto_changed:
                    conn.execute("""
                        INSERT INTO customers
                            (code, name, salesperson, zone, customer_type,
                             address, phone, fax, tax_id, credit_days, contact,
                             contact_note, contact_orig_json, contact_normalized_at,
                             contact_normalized_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?)
                    """, (c['code'], c['name'], c['salesperson'], c['zone'],
                          c['customer_type'], c.get('address'),
                          out_phone, out_fax, c['tax_id'], c['credit_days'],
                          out_contact, out_note, orig_json, normalized_by))
                else:
                    conn.execute("""
                        INSERT INTO customers
                            (code, name, salesperson, zone, customer_type,
                             address, phone, tax_id, credit_days, contact)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    """, (c['code'], c['name'], c['salesperson'], c['zone'],
                          c['customer_type'], c.get('address'),
                          out_phone, c['tax_id'], c['credit_days'], out_contact))
                inserted += 1

    conn.commit()
    conn.close()
    return inserted, updated, protected


def get_customers_for_map(zone=None, customer_type=None, geocoded_only=False):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if zone:
        conds.append('zone=?'); params.append(zone)
    if customer_type:
        conds.append('customer_type=?'); params.append(customer_type)
    if geocoded_only:
        conds.append('lat IS NOT NULL')
    where = ' AND '.join(conds)
    rows = conn.execute(
        f"SELECT * FROM customers WHERE {where} ORDER BY zone, code",
        params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_customer_geocode(code, lat, lng):
    conn = get_connection()
    conn.execute(
        "UPDATE customers SET lat=?, lng=?, geocoded_at=datetime('now','localtime') WHERE code=?",
        (lat, lng, code)
    )
    conn.commit()
    conn.close()


def get_customer_zones():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT zone FROM customers WHERE zone IS NOT NULL ORDER BY zone"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_customer_types():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT customer_type FROM customers WHERE customer_type IS NOT NULL ORDER BY customer_type"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_geocode_progress():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    geocoded = conn.execute("SELECT COUNT(*) FROM customers WHERE lat IS NOT NULL").fetchone()[0]
    conn.close()
    return total, geocoded
