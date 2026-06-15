"""Customer contact-data review blueprint — /customers/normalize.

Lets Put review the 883 'pending' rows in customer_contact_review one by one,
confirm (writes to customers) or skip them.

Routes
------
GET  /customers/normalize                        — worklist (pending queue)
GET  /customers/normalize/<customer_code>        — detail (side-by-side + editable form)
POST /customers/normalize/<customer_code>/confirm — write proposed fields to customers
POST /customers/normalize/<customer_code>/skip   — mark skipped; don't touch customers
"""
import json

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session)

from database import get_connection

bp_customer_review = Blueprint('customer_review', __name__)


def _get_review_row(conn, customer_code):
    """Return the customer_contact_review row for customer_code, or None."""
    return conn.execute(
        "SELECT * FROM customer_contact_review WHERE customer_code=?",
        (customer_code,),
    ).fetchone()


def _parse_issues(issues_json):
    """Parse issues_json → list of tag strings. Safe on None / bad JSON."""
    if not issues_json:
        return []
    try:
        tags = json.loads(issues_json)
        if isinstance(tags, list):
            return [str(t) for t in tags]
    except (ValueError, TypeError):
        pass
    return []


def _status_counts(conn):
    """Return dict {status: count} for all statuses in customer_contact_review."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM customer_contact_review GROUP BY status"
    ).fetchall()
    counts = {'pending': 0, 'applied': 0, 'confirmed': 0, 'skipped': 0}
    for row in rows:
        counts[row['status']] = row['n']
    return counts


# ── Worklist ──────────────────────────────────────────────────────────────────

@bp_customer_review.route('/customers/normalize')
def normalize_list():
    conn = get_connection()

    status_filter = request.args.get('status', 'pending')
    q = request.args.get('q', '').strip()

    counts = _status_counts(conn)

    # Build query — join customers for the live name
    params = []
    where_clauses = []

    if status_filter and status_filter != 'all':
        where_clauses.append("ccr.status = ?")
        params.append(status_filter)

    if q:
        where_clauses.append(
            "(ccr.customer_code LIKE ? OR ccr.proposed_name LIKE ? OR ccr.original_json LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = conn.execute(f"""
        SELECT
            ccr.id,
            ccr.customer_code,
            ccr.proposed_name,
            ccr.proposed_phone,
            ccr.proposed_fax,
            ccr.original_json,
            ccr.issues_json,
            ccr.confidence,
            ccr.status,
            ccr.reviewed_at,
            c.name AS live_name
        FROM customer_contact_review ccr
        LEFT JOIN customers c ON c.code = ccr.customer_code
        {where_sql}
        ORDER BY
            CASE ccr.status WHEN 'pending' THEN 0 WHEN 'applied' THEN 1
                            WHEN 'confirmed' THEN 2 WHEN 'skipped' THEN 3 ELSE 9 END,
            ccr.customer_code
    """, params).fetchall()

    # Parse issues_json for each row into a list
    display_rows = []
    for row in rows:
        orig = {}
        try:
            orig = json.loads(row['original_json'] or '{}')
        except (ValueError, TypeError):
            pass
        display_rows.append({
            'id': row['id'],
            'customer_code': row['customer_code'],
            'proposed_name': row['proposed_name'],
            'proposed_phone': row['proposed_phone'],
            'proposed_fax': row['proposed_fax'],
            'orig_phone': orig.get('phone', ''),
            'issues': _parse_issues(row['issues_json']),
            'confidence': row['confidence'],
            'status': row['status'],
            'reviewed_at': row['reviewed_at'],
            'live_name': row['live_name'],
        })

    conn.close()
    return render_template(
        'customer_review/list.html',
        rows=display_rows,
        counts=counts,
        status_filter=status_filter,
        q=q,
        args=request.args,
    )


# ── Detail / edit form ────────────────────────────────────────────────────────

@bp_customer_review.route('/customers/normalize/<path:customer_code>')
def normalize_detail(customer_code):
    conn = get_connection()

    review = _get_review_row(conn, customer_code)
    if not review:
        conn.close()
        flash('ไม่พบข้อมูลตรวจสอบสำหรับรหัสลูกค้านี้', 'warning')
        return redirect(url_for('customer_review.normalize_list'))

    live = conn.execute(
        "SELECT * FROM customers WHERE code=?", (customer_code,)
    ).fetchone()

    conn.close()

    orig = {}
    try:
        orig = json.loads(review['original_json'] or '{}')
    except (ValueError, TypeError):
        pass

    issues = _parse_issues(review['issues_json'])

    return render_template(
        'customer_review/detail.html',
        review=review,
        orig=orig,
        issues=issues,
        live=live,
        customer_code=customer_code,
        back_args=request.args,
    )


# ── Confirm (write to customers) ──────────────────────────────────────────────

@bp_customer_review.route('/customers/normalize/<path:customer_code>/confirm',
                          methods=['POST'])
def normalize_confirm(customer_code):
    conn = get_connection()
    f = request.form
    user = session.get('username')

    review = _get_review_row(conn, customer_code)
    if not review:
        conn.close()
        flash('ไม่พบข้อมูลตรวจสอบสำหรับรหัสนี้', 'warning')
        return redirect(url_for('customer_review.normalize_list'))

    # Read submitted fields; empty string → None
    proposed_name    = f.get('proposed_name', '').strip() or None
    proposed_nickname = f.get('proposed_nickname', '').strip() or None
    proposed_phone   = f.get('proposed_phone', '').strip() or None
    proposed_fax     = f.get('proposed_fax', '').strip() or None
    proposed_contact = f.get('proposed_contact', '').strip() or None
    proposed_address = f.get('proposed_address', '').strip() or None

    # name must not be NULL — fall back to existing name if blank
    if proposed_name is None:
        live_row = conn.execute(
            "SELECT name FROM customers WHERE code=?", (customer_code,)
        ).fetchone()
        if live_row:
            proposed_name = live_row['name']
            flash('ชื่อลูกค้าว่างเปล่า — ใช้ชื่อเดิมแทน', 'warning')
        else:
            proposed_name = customer_code  # last resort

    # Freeze original_json once (COALESCE keeps existing snapshot)
    orig_json = review['original_json']  # already a JSON string

    cur = conn.execute("""
        UPDATE customers
        SET name                   = ?,
            nickname               = ?,
            phone                  = ?,
            fax                    = ?,
            contact                = ?,
            address                = ?,
            contact_orig_json      = COALESCE(contact_orig_json, ?),
            contact_normalized_at  = datetime('now','localtime'),
            contact_normalized_by  = ?
        WHERE code = ?
    """, (
        proposed_name,
        proposed_nickname,
        proposed_phone,
        proposed_fax,
        proposed_contact,
        proposed_address,
        orig_json,
        user,
        customer_code,
    ))

    if cur.rowcount == 0:
        flash(f'ไม่พบลูกค้า {customer_code} ใน customers — ข้ามการบันทึก', 'warning')
    else:
        # Also store the confirmed values into proposed_* columns so the record
        # reflects what was actually saved.
        conn.execute("""
            UPDATE customer_contact_review
            SET status           = 'confirmed',
                reviewed_by      = ?,
                reviewed_at      = datetime('now','localtime'),
                proposed_name    = ?,
                proposed_nickname = ?,
                proposed_phone   = ?,
                proposed_fax     = ?,
                proposed_contact = ?,
                proposed_address = ?
            WHERE customer_code = ?
        """, (
            user,
            proposed_name,
            proposed_nickname,
            proposed_phone,
            proposed_fax,
            proposed_contact,
            proposed_address,
            customer_code,
        ))
        flash(f'บันทึกข้อมูลติดต่อสำหรับ {proposed_name} แล้ว', 'success')

    conn.commit()
    conn.close()

    # Redirect back to the worklist (preserve status filter)
    return redirect(url_for('customer_review.normalize_list',
                            status=request.form.get('back_status', 'pending')))


# ── Skip ─────────────────────────────────────────────────────────────────────

@bp_customer_review.route('/customers/normalize/<path:customer_code>/skip',
                          methods=['POST'])
def normalize_skip(customer_code):
    conn = get_connection()
    user = session.get('username')

    conn.execute("""
        UPDATE customer_contact_review
        SET status      = 'skipped',
            reviewed_by = ?,
            reviewed_at = datetime('now','localtime')
        WHERE customer_code = ?
    """, (user, customer_code))
    conn.commit()
    conn.close()

    flash('ข้ามลูกค้านี้ไว้ก่อน', 'info')
    return redirect(url_for('customer_review.normalize_list',
                            status=request.form.get('back_status', 'pending')))
