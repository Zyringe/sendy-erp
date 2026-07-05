"""Commission-override CRUD helpers — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Distinct from the top-level `commission.py` (payroll/commission-calculation
engine) and `blueprints/commission_bp.py` (routes) — this module is
`models.commission`, the DB-row CRUD for commission_overrides only.
"""

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
        cur = conn.execute("""
            INSERT INTO commission_overrides
                (product_id, brand_id, salesperson_code,
                 fixed_per_unit, custom_rate_pct,
                 apply_when_price_gt, apply_when_price_lte,
                 is_active, effective_from, note)
            VALUES (:product_id, :brand_id, :salesperson_code,
                    :fixed_per_unit, :custom_rate_pct,
                    :apply_when_price_gt, :apply_when_price_lte,
                    :is_active, COALESCE(:effective_from, date('now')), :note)
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
