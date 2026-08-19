"""Commission-override CRUD helpers — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Distinct from the top-level `commission.py` (payroll/commission-calculation
engine) and `blueprints/commission_bp.py` (routes) — this module is
`models.commission`, the DB-row CRUD for commission_overrides only.
"""

import sqlite3
from datetime import date as _date

from database import get_connection


def _normalise_override_payload(data):
    """Coerce form values into the shape stored in DB. Returns
    (normalised_dict, error_str_or_None)."""
    scope = (data.get('scope') or '').strip()
    rate_kind = (data.get('rate_kind') or '').strip()

    out = {
        'product_id':           None,
        'brand_id':              None,
        'salesperson_code':      None,
        'fixed_per_unit':        None,
        'custom_rate_pct':       None,
        'apply_when_price_gt':   0.0,
        'apply_when_price_lte':  None,
        'is_active':             1,
        'effective_from':        (data.get('effective_from') or '').strip() or None,
        'note':                  (data.get('note') or '').strip() or None,
    }

    if scope == 'product':
        pid_raw = (data.get('product_id') or '').strip()
        if not pid_raw.isdigit():
            return None, 'กรุณาเลือกสินค้า'
        out['product_id'] = int(pid_raw)
    elif scope == 'brand':
        bid_raw = (data.get('brand_id') or '').strip()
        if not bid_raw.isdigit():
            return None, 'กรุณาเลือกแบรนด์'
        out['brand_id'] = int(bid_raw)
    else:
        return None, 'กรุณาเลือก scope (product / brand)'

    if rate_kind == 'fixed':
        try:
            v = float((data.get('fixed_per_unit') or '').strip())
        except ValueError:
            return None, 'fixed_per_unit ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'fixed_per_unit ต้อง ≥ 0'
        out['fixed_per_unit'] = v
    elif rate_kind == 'percent':
        try:
            v = float((data.get('custom_rate_pct') or '').strip())
        except ValueError:
            return None, 'custom_rate_pct ต้องเป็นตัวเลข'
        if v < 0 or v > 100:
            return None, 'custom_rate_pct ต้องอยู่ระหว่าง 0 และ 100'
        out['custom_rate_pct'] = v
    else:
        return None, 'กรุณาเลือกประเภทอัตรา (fixed / percentage)'

    sp = (data.get('salesperson_code') or '').strip() or None
    if sp:
        out['salesperson_code'] = sp

    gt_raw = (data.get('apply_when_price_gt') or '').strip()
    if gt_raw:
        try:
            v = float(gt_raw)
        except ValueError:
            return None, 'price_gt ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'price_gt ต้อง ≥ 0'
        out['apply_when_price_gt'] = v

    lte_raw = (data.get('apply_when_price_lte') or '').strip()
    if lte_raw:
        try:
            v = float(lte_raw)
        except ValueError:
            return None, 'price_lte ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'price_lte ต้อง ≥ 0'
        if v <= out['apply_when_price_gt']:
            return None, 'price_lte ต้องมากกว่า price_gt'
        out['apply_when_price_lte'] = v

    out['is_active'] = 1 if (data.get('is_active') in (1, '1', 'on', True)) else 0
    return out, None


def _validate_override_targets(conn, payload):
    if payload['product_id'] is not None:
        if not conn.execute(
            "SELECT 1 FROM products WHERE id = ?", (payload['product_id'],)
        ).fetchone():
            return f'ไม่พบ product id {payload["product_id"]}'
    if payload['brand_id'] is not None:
        if not conn.execute(
            "SELECT 1 FROM brands WHERE id = ?", (payload['brand_id'],)
        ).fetchone():
            return f'ไม่พบ brand id {payload["brand_id"]}'
    if payload['salesperson_code'] is not None:
        if not conn.execute(
            "SELECT 1 FROM salespersons WHERE code = ?", (payload['salesperson_code'],)
        ).fetchone():
            return f'ไม่พบ salesperson code "{payload["salesperson_code"]}"'
    return None


def list_commission_overrides(active_only=False):
    conn = get_connection()
    where = "WHERE co.is_active = 1" if active_only else ""
    sql = f"""
        SELECT co.id, co.product_id, co.brand_id, co.salesperson_code,
               co.fixed_per_unit, co.custom_rate_pct,
               co.apply_when_price_gt, co.apply_when_price_lte,
               co.is_active, co.effective_from, co.note,
               co.created_at, co.updated_at,
               p.product_name,
               b.name AS brand_name, b.code AS brand_code, b.is_own_brand,
               s.name AS salesperson_name
          FROM commission_overrides co
          LEFT JOIN products     p ON p.id   = co.product_id
          LEFT JOIN brands       b ON b.id   = co.brand_id
          LEFT JOIN salespersons s ON s.code = co.salesperson_code
          {where}
         ORDER BY co.is_active DESC, co.id DESC
    """
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_commission_override(override_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT co.*, p.product_name,
               b.name AS brand_name, b.code AS brand_code,
               s.name AS salesperson_name
          FROM commission_overrides co
          LEFT JOIN products     p ON p.id   = co.product_id
          LEFT JOIN brands       b ON b.id   = co.brand_id
          LEFT JOIN salespersons s ON s.code = co.salesperson_code
         WHERE co.id = ?
    """, (override_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_commission_override(form_data):
    payload, err = _normalise_override_payload(form_data)
    if err:
        return {'ok': False, 'id': None, 'error': err}

    conn = get_connection()
    try:
        err = _validate_override_targets(conn, payload)
        if err:
            return {'ok': False, 'id': None, 'error': err}
        # effective_from is a BUSINESS date -- it decides which sales this
        # override applies to, i.e. what a rep gets paid. 'localtime' is
        # load-bearing: app.py sets TZ='ICT-7'+tzset(), so it reads Bangkok,
        # while a bare date('now') is always UTC and would back-date the
        # override by a day for anything created between 00:00 and 07:00.
        # (The column DEFAULT in mig 018/019 has the same UTC shape, but it
        # only fires on an INSERT that omits the column; every write goes
        # through this COALESCE. Not worth a table rebuild to change.)
        cur = conn.execute("""
            INSERT INTO commission_overrides
                (product_id, brand_id, salesperson_code,
                 fixed_per_unit, custom_rate_pct,
                 apply_when_price_gt, apply_when_price_lte,
                 is_active, effective_from, note)
            VALUES (:product_id, :brand_id, :salesperson_code,
                    :fixed_per_unit, :custom_rate_pct,
                    :apply_when_price_gt, :apply_when_price_lte,
                    :is_active, COALESCE(:effective_from, date('now','localtime')), :note)
        """, payload)
        conn.commit()
        return {'ok': True, 'id': cur.lastrowid, 'error': None}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'id': None, 'error': f'ข้อมูลไม่ตรงตามข้อกำหนด: {e}'}
    finally:
        conn.close()


def update_commission_override(override_id, form_data):
    payload, err = _normalise_override_payload(form_data)
    if err:
        return {'ok': False, 'error': err}

    conn = get_connection()
    try:
        if not conn.execute(
            "SELECT 1 FROM commission_overrides WHERE id = ?", (override_id,)
        ).fetchone():
            return {'ok': False, 'error': f'ไม่พบ override id {override_id}'}
        err = _validate_override_targets(conn, payload)
        if err:
            return {'ok': False, 'error': err}
        payload_with_id = dict(payload)
        payload_with_id['id'] = override_id
        conn.execute("""
            UPDATE commission_overrides
               SET product_id           = :product_id,
                   brand_id             = :brand_id,
                   salesperson_code     = :salesperson_code,
                   fixed_per_unit       = :fixed_per_unit,
                   custom_rate_pct      = :custom_rate_pct,
                   apply_when_price_gt  = :apply_when_price_gt,
                   apply_when_price_lte = :apply_when_price_lte,
                   is_active            = :is_active,
                   effective_from       = COALESCE(:effective_from, effective_from),
                   note                 = :note,
                   updated_at           = datetime('now','localtime')
             WHERE id = :id
        """, payload_with_id)
        conn.commit()
        return {'ok': True, 'error': None}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'error': f'ข้อมูลไม่ตรงตามข้อกำหนด: {e}'}
    finally:
        conn.close()


def toggle_commission_override(override_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_active FROM commission_overrides WHERE id = ?", (override_id,)
        ).fetchone()
        if row is None:
            return {'ok': False, 'is_active': None, 'error': f'ไม่พบ override id {override_id}'}
        new_state = 0 if row['is_active'] else 1
        conn.execute(
            "UPDATE commission_overrides SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_state, override_id),
        )
        conn.commit()
        return {'ok': True, 'is_active': new_state, 'error': None}
    finally:
        conn.close()


def delete_commission_override(override_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM commission_overrides WHERE id = ?", (override_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบ override id {override_id}'}
        return {'ok': True, 'error': None}
    finally:
        conn.close()


# ── Customer reassignment CRUD (migration 143) ───────────────────────────────
# Distinct from the overrides above: those change the RATE, these change WHO a
# customer's orders belong to. Attribution semantics (invoice-date keyed,
# latest-rule-wins) live in `commission_attribution.py`.

def _normalise_reassign_payload(data):
    """Coerce form values. Returns (normalised_dict, error_str_or_None)."""
    customer_code = (data.get('customer_code') or '').strip()
    to_salesperson = (data.get('to_salesperson') or '').strip()
    effective_from = (data.get('effective_from') or '').strip()
    note = (data.get('note') or '').strip() or None

    if not customer_code:
        return None, 'ต้องเลือกลูกค้า'
    if not to_salesperson:
        return None, 'ต้องเลือกเซลส์ปลายทาง'
    if not effective_from:
        return None, 'ต้องระบุวันที่เริ่มมีผล'
    # Compared as a plain string against sales_transactions.date_iso, so an
    # impossible-but-well-shaped date ('2026-13-45') would never match any
    # invoice — the rule would sit in the list looking active while changing
    # nothing. Parse it for real so that fails loudly at entry instead.
    try:
        _date.fromisoformat(effective_from)
    except (ValueError, TypeError):
        return None, 'วันที่เริ่มมีผลต้องเป็นวันที่จริงในรูปแบบ YYYY-MM-DD'

    is_active = 1 if str(data.get('is_active', '1')).strip() in ('1', 'on', 'true') else 0
    return {
        'customer_code': customer_code,
        'to_salesperson': to_salesperson,
        'effective_from': effective_from,
        'is_active': is_active,
        'note': note,
    }, None


def _validate_reassign_targets(conn, payload):
    if not conn.execute("SELECT 1 FROM customers WHERE code = ?",
                        (payload['customer_code'],)).fetchone():
        return f"ไม่พบลูกค้ารหัส {payload['customer_code']}"
    if not conn.execute("SELECT 1 FROM salespersons WHERE code = ?",
                        (payload['to_salesperson'],)).fetchone():
        return f"ไม่พบเซลส์รหัส {payload['to_salesperson']}"
    return None


def paid_cycles_affected_by_reassign(conn, customer_code, effective_from, to_salesperson):
    """Cycles that ALREADY have a commission payout and would lose base to this
    rule. Returns [{'year_month', 'losing_rep', 'amount_paid'}], newest first.

    Commission payouts are NOT rewritten by a reassignment — the payout row
    keeps its own salesperson_code. So a rule dated back into a cycle that was
    already paid leaves that cycle showing an overpayment (verified: dating
    ไทยทวีกิจ to 2026-01-01 turns Feb 2026 into −฿915.66 against rep 31).

    That can be a deliberate choice, so callers WARN rather than block — but it
    must never happen by accident, which is what it did before this existed.
    """
    # `commission_payouts` holds one row PER INVOICE, so joining it directly
    # yields a row per payout and the caller would flash the same cycle 18
    # times. Find the distinct affected cycles first, then total each cycle's
    # payouts in a scalar subquery.
    rows = conn.execute("""
        SELECT affected.year_month,
               affected.losing_rep,
               (SELECT ROUND(SUM(cp.amount_paid), 2)
                  FROM commission_payouts cp
                 WHERE cp.salesperson_code = affected.losing_rep
                   AND cp.year_month       = affected.year_month) AS amount_paid
          FROM (SELECT DISTINCT substr(rp.date_iso, 1, 7) AS year_month,
                       rp.salesperson                     AS losing_rep
                  FROM sales_transactions st
                  JOIN paid_invoices pi ON pi.doc_no = st.doc_base
                                       AND pi.doc_kind = 'IV'
                                       AND pi.amount IS NOT NULL AND pi.amount <> 0
                  JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
                 WHERE st.customer_code = ?
                   AND st.date_iso >= ?
                   AND rp.salesperson <> ?
                   AND EXISTS (SELECT 1 FROM commission_payouts cp2
                                WHERE cp2.salesperson_code = rp.salesperson
                                  AND cp2.year_month = substr(rp.date_iso, 1, 7))
               ) affected
         ORDER BY affected.year_month DESC
    """, (customer_code, effective_from, to_salesperson)).fetchall()
    return [dict(r) for r in rows]


def sync_customer_master_salesperson(conn, customer_code):
    """Point `customers.salesperson` at the customer's CURRENT owner, derived
    from its latest active reassignment rule. Returns the new code, or None
    when nothing changed.

    Why this exists (Put, 2026-07-30): the two fields answer different
    questions — `customers.salesperson` is "who looks after this customer"
    (display, zoning, sales trips) and the rule table is "who earns the
    commission" — but in practice they move together: a rule is only ever
    created because the rep really did change. Leaving the master stale is what
    produced the original bug, where three customers read '00' from 2026-04-24
    while their commission still went to rep 31 for months.

    Latest rule wins, matching how the engine resolves attribution, so a
    customer that moves 31 -> 00 -> 02 ends up on 02.

    ⚠ Deliberately NOT reverted when the last rule is deleted or deactivated:
    the pre-rule value is not stored anywhere, and guessing would overwrite a
    correct value with a wrong one. The list page flags any customer whose
    master disagrees with its rule, so the mismatch stays visible.

    Caller owns the transaction — this does not commit.
    """
    row = conn.execute("""
        SELECT to_salesperson FROM commission_customer_reassign
         WHERE customer_code = ? AND is_active = 1
         ORDER BY effective_from DESC, id DESC
         LIMIT 1
    """, (customer_code,)).fetchone()
    if row is None:
        return None                      # no active rule left — leave it alone
    target = row['to_salesperson']
    cur = conn.execute(
        "UPDATE customers SET salesperson = ? WHERE code = ? AND "
        "COALESCE(salesperson, '') <> ?", (target, customer_code, target))
    return target if cur.rowcount else None


def list_customer_reassignments(active_only=False):
    conn = get_connection()
    where = "WHERE r.is_active = 1" if active_only else ""
    rows = conn.execute(f"""
        SELECT r.id, r.customer_code, r.to_salesperson, r.effective_from,
               r.is_active, r.note, r.created_at, r.updated_at,
               c.name AS customer_name,
               c.salesperson AS customer_master_salesperson,
               s.name AS to_salesperson_name,
               -- Does the target rep earn anything? A rep with no
               -- commission_assignments row (e.g. '00' = the company) earns 0,
               -- which is the point of reassigning to it. Surfaced so the UI
               -- can say so rather than leaving it to be inferred.
               (SELECT COUNT(*) FROM commission_assignments a
                 WHERE a.salesperson_code = r.to_salesperson) AS to_has_tier
          FROM commission_customer_reassign r
          LEFT JOIN customers    c ON c.code = r.customer_code
          LEFT JOIN salespersons s ON s.code = r.to_salesperson
          {where}
         ORDER BY r.is_active DESC, c.name, r.effective_from DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_reassignment(reassign_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT r.*, c.name AS customer_name, s.name AS to_salesperson_name
          FROM commission_customer_reassign r
          LEFT JOIN customers    c ON c.code = r.customer_code
          LEFT JOIN salespersons s ON s.code = r.to_salesperson
         WHERE r.id = ?
    """, (reassign_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_customer_reassignment(form_data):
    payload, err = _normalise_reassign_payload(form_data)
    if err:
        return {'ok': False, 'id': None, 'error': err, 'paid_cycle_warnings': []}
    conn = get_connection()
    try:
        err = _validate_reassign_targets(conn, payload)
        if err:
            return {'ok': False, 'id': None, 'error': err, 'paid_cycle_warnings': []}
        warnings = paid_cycles_affected_by_reassign(
            conn, payload['customer_code'], payload['effective_from'],
            payload['to_salesperson'])
        cur = conn.execute("""
            INSERT INTO commission_customer_reassign
                (customer_code, to_salesperson, effective_from, is_active, note)
            VALUES (:customer_code, :to_salesperson, :effective_from, :is_active, :note)
        """, payload)
        synced = sync_customer_master_salesperson(conn, payload['customer_code'])
        conn.commit()
        return {'ok': True, 'id': cur.lastrowid, 'error': None,
                'paid_cycle_warnings': warnings, 'master_synced_to': synced}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'id': None, 'paid_cycle_warnings': [],
                'error': f'มีกฎของลูกค้ารายนี้ที่วันที่เดียวกันอยู่แล้ว ({e})'}
    finally:
        conn.close()


def update_customer_reassignment(reassign_id, form_data):
    payload, err = _normalise_reassign_payload(form_data)
    if err:
        return {'ok': False, 'error': err, 'paid_cycle_warnings': []}
    conn = get_connection()
    try:
        err = _validate_reassign_targets(conn, payload)
        if err:
            return {'ok': False, 'error': err, 'paid_cycle_warnings': []}
        warnings = paid_cycles_affected_by_reassign(
            conn, payload['customer_code'], payload['effective_from'],
            payload['to_salesperson'])
        payload['id'] = reassign_id
        cur = conn.execute("""
            UPDATE commission_customer_reassign
               SET customer_code  = :customer_code,
                   to_salesperson = :to_salesperson,
                   effective_from = :effective_from,
                   is_active      = :is_active,
                   note           = :note,
                   updated_at     = datetime('now','localtime')
             WHERE id = :id
        """, payload)
        conn.commit()
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบกฎ id {reassign_id}',
                    'paid_cycle_warnings': []}
        synced = sync_customer_master_salesperson(conn, payload['customer_code'])
        conn.commit()
        return {'ok': True, 'error': None, 'paid_cycle_warnings': warnings,
                'master_synced_to': synced}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'paid_cycle_warnings': [],
                'error': f'มีกฎของลูกค้ารายนี้ที่วันที่เดียวกันอยู่แล้ว ({e})'}
    finally:
        conn.close()


def toggle_customer_reassignment(reassign_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_active FROM commission_customer_reassign WHERE id = ?",
            (reassign_id,)).fetchone()
        if row is None:
            return {'ok': False, 'is_active': None, 'error': f'ไม่พบกฎ id {reassign_id}'}
        new_state = 0 if row['is_active'] else 1
        conn.execute(
            "UPDATE commission_customer_reassign SET is_active = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (new_state, reassign_id))
        cc = conn.execute("SELECT customer_code FROM commission_customer_reassign "
                          "WHERE id = ?", (reassign_id,)).fetchone()
        synced = sync_customer_master_salesperson(conn, cc['customer_code']) if cc else None
        conn.commit()
        return {'ok': True, 'is_active': new_state, 'error': None,
                'master_synced_to': synced}
    finally:
        conn.close()


def delete_customer_reassignment(reassign_id):
    conn = get_connection()
    try:
        cc = conn.execute("SELECT customer_code FROM commission_customer_reassign "
                          "WHERE id = ?", (reassign_id,)).fetchone()
        cur = conn.execute(
            "DELETE FROM commission_customer_reassign WHERE id = ?", (reassign_id,))
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบกฎ id {reassign_id}'}
        synced = sync_customer_master_salesperson(conn, cc['customer_code']) if cc else None
        conn.commit()
        return {'ok': True, 'error': None, 'master_synced_to': synced}
    finally:
        conn.close()
