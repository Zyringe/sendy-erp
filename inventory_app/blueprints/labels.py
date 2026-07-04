"""Product-label (ป้ายสินค้า) blueprint — Phase 2 admin manage/edit UI +
Phase 3 team print UI.

Phase 1 (data model + import, migration 127) already shipped `product_labels`
(1,104 rows) + a single `label_company_block` config row — see
projects/product-label-printing/plan.md. Phase 2 is the ADMIN-ONLY screen to
search/filter/edit those rows. Phase 3 is the team-facing print UI (NOT the
old price-tag `/labels` route in app.py, which is a different feature).

Routes
------
GET  /labels/manage                 search/filter list + bulk size-set form
GET  /labels/<id>/edit               edit form
POST /labels/<id>/edit               save one row
POST /labels/bulk-size               set label_size on selected/filtered rows
GET  /labels/company-block           edit form for the single constant-block row
POST /labels/company-block           save
GET  /labels/print                   Phase 3 — search + queue + print (team)
GET  /api/labels/search              Phase 3 — JSON search for the picker

Access: manage/edit/company-block = admin only (plan decision D6 — only Put
edits label data). print/search = admin/manager/staff (plan decision D6 —
everyone except พนักงานทั่วไป(`general`) + `shareholder` prints). Mirrors
hr.py's local `_require_admin()` pattern — no shared helper across blueprints.

Python 3.9 — Optional[...] not X | None.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, abort, jsonify)

from database import get_connection

bp_labels = Blueprint('labels', __name__)

_PER_PAGE = 50


def _require_admin():
    if session.get('role') != 'admin':
        abort(403)


def _require_print_role():
    if session.get('role') not in ('admin', 'manager', 'staff'):
        abort(403)


def _where_clause(q, review):
    """Shared WHERE builder for the list query and the bulk "apply to all
    filtered" scope — keeps the two selections in sync."""
    where = ["is_active = 1"]
    params = {}
    if q:
        where.append("(product_name LIKE :q OR barcode LIKE :q OR brand LIKE :q)")
        params['q'] = f'%{q}%'
    if review == 'flagged':
        where.append("needs_review = 1")
    elif review == 'ok':
        where.append("needs_review = 0")
    return " AND ".join(where), params


def _list_labels(conn, q, review, page):
    where_sql, params = _where_clause(q, review)
    total = conn.execute(
        f"SELECT COUNT(*) FROM product_labels WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT id, barcode, product_name, brand, label_size, needs_review, review_note
              FROM product_labels
             WHERE {where_sql}
             ORDER BY id
             LIMIT :lim OFFSET :off""",
        {**params, 'lim': _PER_PAGE, 'off': (page - 1) * _PER_PAGE},
    ).fetchall()
    pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    return rows, total, pages


# ── List / search ────────────────────────────────────────────────────────────

@bp_labels.route('/labels/manage')
def manage():
    _require_admin()
    q = request.args.get('q', '').strip()
    review = request.args.get('review', '')
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    conn = get_connection()
    try:
        rows, total, pages = _list_labels(conn, q, review, page)
        flagged_total = conn.execute(
            "SELECT COUNT(*) FROM product_labels WHERE is_active = 1 AND needs_review = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return render_template(
        'labels/manage.html', rows=rows, total=total, page=page, pages=pages,
        q=q, review=review, flagged_total=flagged_total,
    )


# ── Edit one row ─────────────────────────────────────────────────────────────

@bp_labels.route('/labels/<int:label_id>/edit', methods=['GET', 'POST'])
def edit(label_id):
    _require_admin()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM product_labels WHERE id = ?", (label_id,)
        ).fetchone()
        if not row:
            abort(404)
        if request.method == 'POST':
            f = request.form
            conn.execute(
                """UPDATE product_labels
                      SET product_name = ?, brand = ?, barcode = ?,
                          usage_th = ?, warning_th = ?, packaging_th = ?, size_th = ?,
                          label_size = ?, needs_review = ?, review_note = ?,
                          updated_at = datetime('now')
                    WHERE id = ?""",
                (f.get('product_name', '').strip(),
                 f.get('brand', '').strip(),
                 f.get('barcode', '').strip(),
                 f.get('usage_th', '').strip(),
                 f.get('warning_th', '').strip(),
                 f.get('packaging_th', '').strip(),
                 f.get('size_th', '').strip(),
                 f.get('label_size', 'big'),
                 1 if f.get('needs_review') else 0,
                 f.get('review_note', '').strip() or None,
                 label_id),
            )
            conn.commit()
            flash('บันทึกป้ายสินค้าเรียบร้อย', 'success')
            return redirect(url_for(
                'labels.manage',
                q=f.get('back_q', ''), review=f.get('back_review', ''),
                page=f.get('back_page', 1),
            ))
        return render_template('labels/edit.html', row=row)
    finally:
        conn.close()


# ── Bulk size-set ────────────────────────────────────────────────────────────

@bp_labels.route('/labels/bulk-size', methods=['POST'])
def bulk_size():
    _require_admin()
    f = request.form
    label_size = f.get('label_size')
    back = dict(q=f.get('q', ''), review=f.get('review', ''), page=f.get('page', 1))
    if label_size not in ('small', 'big'):
        flash('ขนาดไม่ถูกต้อง', 'danger')
        return redirect(url_for('labels.manage', **back))

    conn = get_connection()
    try:
        if f.get('scope') == 'filtered':
            where_sql, params = _where_clause(f.get('q', '').strip(), f.get('review', ''))
            cur = conn.execute(
                f"UPDATE product_labels SET label_size = :size, updated_at = datetime('now') "
                f"WHERE {where_sql}",
                {**params, 'size': label_size},
            )
        else:
            ids = [int(i) for i in f.getlist('label_ids') if i.isdigit()]
            if not ids:
                flash('ยังไม่ได้เลือกรายการ', 'warning')
                return redirect(url_for('labels.manage', **back))
            placeholders = ','.join('?' * len(ids))
            cur = conn.execute(
                f"UPDATE product_labels SET label_size = ?, updated_at = datetime('now') "
                f"WHERE id IN ({placeholders})",
                [label_size] + ids,
            )
        conn.commit()
        flash(f'ตั้งขนาดป้าย {cur.rowcount} รายการเรียบร้อย', 'success')
    finally:
        conn.close()
    return redirect(url_for('labels.manage', **back))


# ── Company block (single-row config) ───────────────────────────────────────

_COMPANY_BLOCK_FIELDS = (
    'distributor_th', 'importer_th', 'address_th',
    'importer_addr1_th', 'importer_addr2_th', 'country_th',
    'quality_th', 'price_line_th',
)


@bp_labels.route('/labels/company-block', methods=['GET', 'POST'])
def company_block():
    _require_admin()
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM label_company_block ORDER BY id LIMIT 1").fetchone()
        if request.method == 'POST':
            values = [request.form.get(k, '').strip() for k in _COMPANY_BLOCK_FIELDS]
            if row:
                set_sql = ", ".join(f"{k} = ?" for k in _COMPANY_BLOCK_FIELDS)
                conn.execute(
                    f"UPDATE label_company_block SET {set_sql}, updated_at = datetime('now') "
                    f"WHERE id = ?",
                    values + [row['id']],
                )
            else:
                cols = ", ".join(_COMPANY_BLOCK_FIELDS)
                qmarks = ", ".join('?' * len(_COMPANY_BLOCK_FIELDS))
                conn.execute(
                    f"INSERT INTO label_company_block ({cols}) VALUES ({qmarks})", values
                )
            conn.commit()
            flash('บันทึกข้อมูลบริษัท (คงที่) เรียบร้อย', 'success')
            return redirect(url_for('labels.company_block'))
        return render_template('labels/company_block.html', row=row)
    finally:
        conn.close()


# ── Phase 3: team print UI ──────────────────────────────────────────────────

@bp_labels.route('/labels/print')
def print_page():
    _require_print_role()
    conn = get_connection()
    try:
        company = conn.execute(
            "SELECT * FROM label_company_block ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return render_template('labels/print.html', company=company)


@bp_labels.route('/api/labels/search')
def search_api():
    _require_print_role()
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'items': []})
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, barcode, product_name, brand, packaging_th, size_th, label_size,
                   usage_th, warning_th
              FROM product_labels
             WHERE is_active = 1
               AND (product_name LIKE :q OR barcode LIKE :q OR brand LIKE :q)
             ORDER BY
                 CASE WHEN barcode = :exact THEN 0
                      WHEN product_name LIKE :starts THEN 1
                      ELSE 2 END,
                 product_name
             LIMIT 30
            """,
            {'q': f'%{q}%', 'starts': f'{q}%', 'exact': q},
        ).fetchall()
    finally:
        conn.close()
    items = [{
        'id':           r['id'],
        'barcode':      r['barcode'] or '',
        'product_name': r['product_name'],
        'brand':        r['brand'] or '',
        'packaging_th': r['packaging_th'] or '',
        'size_th':      r['size_th'] or '',
        'label_size':   r['label_size'],
        'usage_th':     r['usage_th'] or '',
        'warning_th':   r['warning_th'] or '',
    } for r in rows]
    return jsonify({'items': items})
