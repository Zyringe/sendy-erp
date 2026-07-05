"""Customers + regions + BSN customer-master import + geocode — extracted
verbatim from models.py (behavior-preserving split, Phase 11) — see
models/__init__.py's module docstring for the overall file-split
rationale. No behavior changes.
"""
import json
from database import get_connection


def get_customer_summary(customer, date_from=None, date_to=None):
    """
    Returns summary + top products + monthly trend for a specific customer.
    """
    conn = get_connection()
    conds = ['customer = ?']
    params = [customer]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

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


def get_customers(search=None, region=None, region_id=None, page=1, per_page=50):
    """Customer list backed by customers master + salespersons + regions.

    Filter precedence: region_id (FK, new) > region (text, legacy URL).
    Returns customer rows with display fields:
        salesperson  → name from salespersons master, or raw code if orphan
        region       → name_th from regions, or code as fallback
    """
    conn = get_connection()
    conds = []
    params = []
    if search:
        conds.append("(s.customer LIKE ? OR s.customer_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

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
        params.append(rid_int)

    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT s.customer, s.customer_code,
               COALESCE(r.name_th, r.code)              AS region,
               r.code                                   AS region_code,
               c.region_id,
               COALESCE(sp.name, c.salesperson)         AS salesperson,
               c.salesperson                            AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)                  AS salesperson_orphan,
               COUNT(DISTINCT s.doc_no)                 AS doc_count,
               COALESCE(SUM(s.net), 0)                  AS total_net,
               MAX(s.date_iso)                          AS last_date,
               (c.code IS NULL)                         AS missing_master
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        LEFT JOIN regions       r  ON r.id    = c.region_id
        {where}
        GROUP BY s.customer_code
        ORDER BY s.customer
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        SELECT COUNT(DISTINCT s.customer_code)
        FROM sales_transactions s
        LEFT JOIN customers c ON c.code = s.customer_code
        {where}
    """
    total = conn.execute(count_sql, params).fetchone()[0]
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


def bulk_reassign_customers(customer_codes, salesperson_code, region_id, mode='both'):
    if mode not in ('salesperson', 'region', 'both'):
        return {'ok': False, 'updated': 0, 'error': 'mode ไม่ถูกต้อง'}
    if not customer_codes:
        return {'ok': False, 'updated': 0, 'error': 'ไม่มีลูกค้าที่เลือก'}
    if len(customer_codes) > _BULK_MAX:
        return {'ok': False, 'updated': 0,
                'error': f'เลือกได้สูงสุด {_BULK_MAX} ลูกค้า (เลือก {len(customer_codes)})'}

    sp = (salesperson_code or '').strip() or None
    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is not None:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            return {'ok': False, 'updated': 0, 'error': 'region_id ไม่ถูกต้อง'}

    # Block silent mass-NULL clearing: when a column is in scope it must have a
    # non-empty target. (Future feature can add an explicit "clear" mode.)
    if mode in ('salesperson', 'both') and sp is None:
        return {'ok': False, 'updated': 0, 'error': 'กรุณาเลือก salesperson ปลายทาง'}
    if mode in ('region', 'both') and rid is None:
        return {'ok': False, 'updated': 0, 'error': 'กรุณาเลือก region ปลายทาง'}

    conn = get_connection()
    try:
        if mode in ('salesperson', 'both'):
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'updated': 0,
                        'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}
        if mode in ('region', 'both'):
            if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
                return {'ok': False, 'updated': 0, 'error': f'ไม่พบ region id {rid}'}

        placeholders = ','.join(['?'] * len(customer_codes))
        if mode == 'salesperson':
            sql = f"UPDATE customers SET salesperson = ? WHERE code IN ({placeholders})"
            params = [sp, *customer_codes]
        elif mode == 'region':
            sql = f"UPDATE customers SET region_id = ? WHERE code IN ({placeholders})"
            params = [rid, *customer_codes]
        else:
            sql = (f"UPDATE customers SET salesperson = ?, region_id = ? "
                   f"WHERE code IN ({placeholders})")
            params = [sp, rid, *customer_codes]

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

    for c in customers:
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
