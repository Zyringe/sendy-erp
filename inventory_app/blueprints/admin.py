"""Admin blueprint — user management, DB upload/download, and backup/restore.

Extracted verbatim from app.py (behavior-preserving split) — see app.py's
module docstring for the overall file-split rationale. No URL changes;
route rules are unchanged, only their endpoint names gain an `admin.`
prefix.
"""
import os
import signal
import sqlite3
import shutil
import tempfile
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, abort, send_file, current_app, Response)
from werkzeug.security import generate_password_hash

import config
import db_backup
import hr_queries as hrq
from database import get_connection
from access_control import ROLES

bp_admin = Blueprint('admin', __name__)


# ── User management (admin only) ──────────────────────────────────────────────

def _set_account_employee(conn, uid, emp_id):
    """Point at most one employee at this account — 1:1, integrity-safe.

    First clears any employee currently linked to this account, then links the
    chosen one ONLY if it is free (`user_id IS NULL`). The free-only guard means
    a forged/stale employee_id can never steal an employee already linked
    elsewhere. emp_id may be falsy (= "ไม่ผูก": just unlink)."""
    conn.execute("UPDATE employees SET user_id=NULL WHERE user_id=?", (uid,))
    if emp_id:
        conn.execute(
            "UPDATE employees SET user_id=? WHERE id=? AND is_active=1 AND user_id IS NULL",
            (uid, emp_id))


@bp_admin.route('/users')
def user_list():
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    users = conn.execute("""
        SELECT u.*, e.id AS emp_id, e.full_name AS emp_full_name, e.nickname AS emp_nickname
          FROM users u
          LEFT JOIN employees e ON e.user_id = u.id AND e.is_active = 1
         GROUP BY u.id
         ORDER BY u.role, u.username
    """).fetchall()
    # Employees offered for linking: unlinked for the create form; unlinked +
    # the account's own current link for each edit form (the 1:1 rule).
    create_employees = hrq.get_linkable_employees(conn=conn)
    linkable = {u['id']: hrq.get_linkable_employees(user_id=u['id'], conn=conn) for u in users}
    # Active cashbook accounts offered for each user's data-entry default
    # (mig 126) — distinct from employees.default_cashbook_account_id (the
    # per-employee salary pay-from account).
    cashbook_accounts = conn.execute(
        "SELECT id, code, display_name FROM cashbook_accounts"
        " WHERE is_active=1 ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return render_template('users.html', users=users,
                           create_employees=create_employees, linkable=linkable,
                           cashbook_accounts=cashbook_accounts)


@bp_admin.route('/users/new', methods=['POST'])
def user_new():
    if session.get('role') != 'admin':
        abort(403)
    username     = request.form.get('username', '').strip()
    display_name = request.form.get('display_name', '').strip()
    role         = request.form.get('role', 'staff')
    password     = request.form.get('password', '')
    employee_id  = request.form.get('employee_id', '').strip() or None
    if not username or not password:
        flash('กรุณากรอกชื่อผู้ใช้และรหัสผ่าน', 'danger')
        return redirect(url_for('admin.user_list'))
    if role not in ROLES:
        role = 'staff'
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, display_name, role) VALUES (?,?,?,?)",
            (username, generate_password_hash(password, method='pbkdf2:sha256'), display_name or username, role)
        )
        _set_account_employee(conn, cur.lastrowid, employee_id)
        conn.commit()
        flash(f'เพิ่มผู้ใช้ {username} ({ROLES[role]["label"]}) สำเร็จ', 'success')
    except Exception:
        flash(f'ชื่อผู้ใช้ "{username}" ซ้ำในระบบ', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin.user_list'))


@bp_admin.route('/users/<int:uid>/edit', methods=['POST'])
def user_edit(uid):
    if session.get('role') != 'admin':
        abort(403)
    display_name = request.form.get('display_name', '').strip()
    role         = request.form.get('role', 'staff')
    is_active    = 1 if request.form.get('is_active') else 0
    new_password = request.form.get('password', '').strip()
    employee_id  = request.form.get('employee_id', '').strip() or None
    default_cashbook_account_id = request.form.get('default_cashbook_account_id', '').strip() or None
    if role not in ROLES:
        role = 'staff'
    conn = get_connection()
    if new_password:
        conn.execute(
            "UPDATE users SET display_name=?, role=?, is_active=?, password_hash=?,"
            " default_cashbook_account_id=? WHERE id=?",
            (display_name, role, is_active, generate_password_hash(new_password, method='pbkdf2:sha256'),
             default_cashbook_account_id, uid)
        )
    else:
        conn.execute(
            "UPDATE users SET display_name=?, role=?, is_active=?,"
            " default_cashbook_account_id=? WHERE id=?",
            (display_name, role, is_active, default_cashbook_account_id, uid)
        )
    _set_account_employee(conn, uid, employee_id)
    conn.commit()
    conn.close()
    flash('อัปเดตผู้ใช้สำเร็จ', 'success')
    return redirect(url_for('admin.user_list'))


@bp_admin.route('/users/<int:uid>/delete', methods=['POST'])
def user_delete(uid):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    target = conn.execute("SELECT id, role, username FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        flash('ไม่พบผู้ใช้', 'danger')
    elif target['role'] == 'admin':
        flash('ไม่สามารถลบบัญชี Admin ได้', 'danger')
    elif target['id'] == session.get('user_id'):
        flash('ไม่สามารถลบบัญชีของตัวเองได้', 'danger')
    else:
        # Unlink any employee first — employees.user_id REFERENCES users(id) with
        # no ON DELETE rule, so deleting a linked account would otherwise raise a
        # FK error (500). The employee (HR record) is kept.
        conn.execute("UPDATE employees SET user_id=NULL WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        flash(f'ลบผู้ใช้ {target["username"]} สำเร็จ', 'success')
    conn.close()
    return redirect(url_for('admin.user_list'))


# ── Cashbook account management (admin only) ─────────────────────────────────
# The cash/bank accounts behind the /cashbook dashboard cards. Previously seeded
# only by migration; this is the self-service UI. Admin-only, mirroring /users.
# NO schema change — cashbook_accounts already has every column.

@bp_admin.route('/cashbook-accounts')
def cashbook_account_list():
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    # ref_count = every FK referrer (txns + user/employee defaults + salary
    # advances). ref_count==0 ⇒ a hard delete is safe; otherwise the UI offers
    # deactivate instead. The delete route still catches IntegrityError as the
    # real guard — this hint can go stale under concurrent writes.
    accounts = conn.execute("""
        SELECT a.*,
               (SELECT COUNT(*) FROM cashbook_transactions WHERE account_id=a.id) AS txn_count,
               ( (SELECT COUNT(*) FROM cashbook_transactions WHERE account_id=a.id)
                +(SELECT COUNT(*) FROM users WHERE default_cashbook_account_id=a.id)
                +(SELECT COUNT(*) FROM employees WHERE default_cashbook_account_id=a.id)
                +(SELECT COUNT(*) FROM salary_advances WHERE from_account_id=a.id) ) AS ref_count
          FROM cashbook_accounts a
         ORDER BY a.is_transfer ASC, a.sort_order ASC, a.code ASC
    """).fetchall()
    conn.close()
    return render_template('admin_cashbook_accounts.html', accounts=accounts)


def _cashbook_form_fields():
    """Pull the shared account fields from the POST form. Empty strings → None
    so blank optional fields store NULL (a เงินสด account has no bank)."""
    code = request.form.get('code', '').strip()
    return {
        'code':               code,
        'is_transfer':        1 if request.form.get('is_transfer') == '1' else 0,
        'account_owner_name': request.form.get('account_owner_name', '').strip() or None,
        'bank_name':          request.form.get('bank_name', '').strip() or None,
        'bank_account_no':    request.form.get('bank_account_no', '').strip() or None,
        'note':               request.form.get('note', '').strip() or None,
    }


@bp_admin.route('/cashbook-accounts/new', methods=['POST'])
def cashbook_account_new():
    if session.get('role') != 'admin':
        abort(403)
    f = _cashbook_form_fields()
    if not f['code']:
        flash('กรุณากรอกรหัสบัญชี', 'danger')
        return redirect(url_for('admin.cashbook_account_list'))
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO cashbook_accounts(code, account_owner_name, bank_name,"
            " bank_account_no, note, is_transfer) VALUES (?,?,?,?,?,?)",
            (f['code'], f['account_owner_name'], f['bank_name'],
             f['bank_account_no'], f['note'], f['is_transfer'])
        )
        conn.commit()
        flash(f'เพิ่มบัญชี {f["code"]} สำเร็จ', 'success')
    except sqlite3.IntegrityError:
        flash(f'รหัสบัญชี "{f["code"]}" ซ้ำในระบบ', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin.cashbook_account_list'))


@bp_admin.route('/cashbook-accounts/<int:aid>/edit', methods=['POST'])
def cashbook_account_edit(aid):
    if session.get('role') != 'admin':
        abort(403)
    f = _cashbook_form_fields()
    if not f['code']:
        flash('กรุณากรอกรหัสบัญชี', 'danger')
        return redirect(url_for('admin.cashbook_account_list'))
    is_active = 1 if request.form.get('is_active') else 0
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cashbook_accounts SET code=?, account_owner_name=?, bank_name=?,"
            " bank_account_no=?, note=?, is_transfer=?, is_active=?,"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (f['code'], f['account_owner_name'], f['bank_name'], f['bank_account_no'],
             f['note'], f['is_transfer'], is_active, aid)
        )
        conn.commit()
        flash(f'อัปเดตบัญชี {f["code"]} สำเร็จ', 'success')
    except sqlite3.IntegrityError:
        flash(f'รหัสบัญชี "{f["code"]}" ซ้ำในระบบ', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin.cashbook_account_list'))


@bp_admin.route('/cashbook-accounts/<int:aid>/delete', methods=['POST'])
def cashbook_account_delete(aid):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    acct = conn.execute("SELECT code FROM cashbook_accounts WHERE id=?", (aid,)).fetchone()
    if not acct:
        conn.close()
        flash('ไม่พบบัญชี', 'danger')
        return redirect(url_for('admin.cashbook_account_list'))
    try:
        # foreign_keys=ON is the real guard: cashbook_transactions.account_id,
        # users/employees.default_cashbook_account_id and salary_advances
        # .from_account_id all REFERENCE this id, so a referenced account raises
        # here — no need to enumerate the referrers (that list rots).
        conn.execute("DELETE FROM cashbook_accounts WHERE id=?", (aid,))
        conn.commit()
        flash(f'ลบบัญชี {acct["code"]} แล้ว', 'success')
    except sqlite3.IntegrityError:
        conn.rollback()
        flash(f'ลบบัญชี {acct["code"]} ไม่ได้ — บัญชีนี้ถูกอ้างอิงอยู่ '
              '(มีรายการ หรือเป็นบัญชีตั้งต้นของผู้ใช้/พนักงาน) ให้ "ปิดใช้งาน" แทน', 'warning')
    finally:
        conn.close()
    return redirect(url_for('admin.cashbook_account_list'))


# ── Temp: Download DB (ลบออกหลังใช้) ─────────────────────────────────────────

@bp_admin.route('/admin/toggle-db-routes', methods=['POST'])
def toggle_db_routes():
    if session.get('role') != 'admin':
        abort(403)
    session['db_routes_enabled'] = enabled = not session.get('db_routes_enabled', False)
    state = 'เปิด' if enabled else 'ปิด'
    flash(f'{state}การเข้าถึง Upload/Download Database แล้ว', 'success')
    # When DISABLING, request.referrer is usually the Upload/Download page we
    # just locked — bouncing back there would 403. Go to the dashboard instead.
    # When enabling, stay on the referring page so the sidebar links appear.
    if not enabled:
        return redirect(url_for('dashboard'))
    return redirect(request.referrer or url_for('dashboard'))


@bp_admin.route('/admin/download-db')
def download_db():
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        abort(403)
    # Stream a CONSISTENT online-backup snapshot, not the raw live file: prod is
    # WAL mode, so send_file(inventory.db) could omit the newest committed pages
    # still in the -wal sidecar (stale/torn download). The temp lands on the
    # ephemeral fs (not the small /data volume).
    fd, tmp = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        db_backup.snapshot_db(config.DATABASE_PATH, tmp)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    size = os.path.getsize(tmp)

    def _stream_and_cleanup():
        # The generator's finally deletes the temp when the response finishes
        # streaming OR the client disconnects (the WSGI server closes the
        # generator) — reliable where send_file + call_on_close was not.
        try:
            with open(tmp, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    resp = Response(_stream_and_cleanup(), mimetype='application/octet-stream')
    resp.headers['Content-Length'] = str(size)
    resp.headers['Content-Disposition'] = 'attachment; filename="inventory.db"'
    return resp


# Tables compared during /admin/upload-db. If any of these have MORE rows in the
# current (production) DB than in the uploaded file, the upload is blocked
# pending explicit confirmation — those are the tables where data is added
# through the running app, so a higher count = data the upload would erase.
_UPLOAD_DIFF_TABLES = (
    'sales_transactions', 'purchase_transactions',
    'received_payments', 'paid_invoices',
    'product_code_mapping',
    'express_sales', 'express_payments_in', 'express_payments_out',
    'commission_payouts', 'payout_invoices',
    'transactions',
    'products', 'customers',
)

# MASTER_TABLES = tables Put owns on local — replaced from upload in
# master-only mode. Transaction tables and anything else are preserved
# from current prod (whitelist approach: safer to add than to forget).
# Friend's "อัพเดทข้อมูล" tab writes to transaction tables only, so this
# split lets Put push schema/master changes without wiping friend's interim
# transaction uploads.
_MASTER_TABLES = (
    # Schema sync
    'applied_migrations',
    # Product master
    'products',
    'product_families', 'product_images',
    'product_locations', 'product_barcodes',
    'product_price_tiers',
    # Lookup master
    'brands', 'categories', 'color_finish_codes',
    # Mapping master
    'product_code_mapping', 'unit_conversions',
    'conversion_formulas', 'conversion_formula_inputs',
    # Operations master
    'regions', 'customer_regions',
    'expense_categories', 'promotions',
    'platform_skus', 'platform_products', 'ecommerce_listings', 'listing_bundles',
    'po_sequences', 'salespersons',
    'commission_tiers', 'commission_assignments', 'commission_overrides',
    # AR write-off decisions (Put-owned; excluded from collectable AR). Standalone
    # table (no FKs, keyed by doc_no) so load order is free. Must be here or the
    # 58 write-off rows never reach Railway in master-only mode → prod collectable
    # stays wrong while the table sits empty.
    'ar_writeoffs',
    # Supplier master
    'suppliers',
    'supplier_catalogue_items', 'supplier_catalogue_versions',
    'supplier_catalogue_price_history',
    'supplier_product_mapping',
)


def _count_rows(db_path, table):
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
        conn.close()
        return n
    except sqlite3.OperationalError:
        return None  # table doesn't exist


def _diff_db_row_counts(current_path, uploaded_path):
    """Return list of dicts comparing row counts between two DB files.
    Includes a 'warning' flag when the current count exceeds the uploaded count
    (indicating data would be lost on replace)."""
    rows = []
    for table in _UPLOAD_DIFF_TABLES:
        cur = _count_rows(current_path, table)
        upl = _count_rows(uploaded_path, table)
        if cur is None and upl is None:
            continue
        rows.append({
            'table':    table,
            'current':  cur if cur is not None else 0,
            'uploaded': upl if upl is not None else 0,
            'warning':  (cur or 0) > (upl or 0),
            'missing_in_upload': upl is None,
        })
    return rows


def _table_exists(conn, schema, table):
    cur = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _replace_master_tables(current_path, uploaded_path):
    """Replace MASTER tables in current DB with rows from uploaded DB.
    Transaction tables and anything not in _MASTER_TABLES are untouched.

    Single transaction with FK off during replace; FK integrity checked at end.
    On any failure, rolls back and raises — current DB unchanged.
    Returns dict {table: rows_after_replace}.
    """
    conn = sqlite3.connect(current_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ATTACH DATABASE ? AS upl", (uploaded_path,))
        replaced = {}
        skipped = []
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            for table in _MASTER_TABLES:
                if not _table_exists(conn, 'main', table):
                    skipped.append((table, 'missing in current DB'))
                    continue
                if not _table_exists(conn, 'upl', table):
                    skipped.append((table, 'missing in uploaded DB'))
                    continue
                cur.execute(f"DELETE FROM main.{table}")
                cur.execute(f"INSERT INTO main.{table} SELECT * FROM upl.{table}")
                cur.execute(f"SELECT COUNT(*) FROM main.{table}")
                replaced[table] = cur.fetchone()[0]
            # Verify FK integrity before commit
            violations = cur.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                conn.rollback()
                raise RuntimeError(
                    f"FK violations after replace ({len(violations)} total). "
                    f"Sample: {violations[:5]}. No changes applied."
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.OperationalError:
                pass
            raise
        return replaced, skipped
    finally:
        try:
            conn.execute("DETACH DATABASE upl")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.OperationalError:
            pass
        conn.close()


@bp_admin.route('/admin/upload-db', methods=['GET', 'POST'])
def upload_db():
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        abort(403)
    if request.method == 'POST':
        f = request.files.get('db_file')
        if not f or not f.filename.endswith('.db'):
            flash('กรุณาเลือกไฟล์ .db', 'danger')
            return redirect(request.url)

        tmp = tempfile.mktemp(suffix='.db')
        f.save(tmp)

        mode = request.form.get('mode', 'master_only')

        # Master-only mode: replace MASTER tables, preserve transaction tables
        if mode == 'master_only':
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), '..', 'data', 'backups')
            )
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f'inventory-pre-master-upload-{ts}.db')
            try:
                shutil.copy(config.DATABASE_PATH, backup_path)
            except FileNotFoundError:
                backup_path = None

            try:
                replaced, skipped = _replace_master_tables(config.DATABASE_PATH, tmp)
            except Exception as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                flash(f'Master-only upload ล้มเหลว: {e}. DB ปัจจุบันไม่ถูกแก้ไข', 'danger')
                return redirect(url_for('admin.upload_db'))

            try:
                os.remove(tmp)
            except OSError:
                pass

            n = sum(replaced.values())
            msg = (f'Master-only upload สำเร็จ — แทนที่ {len(replaced)} ตาราง '
                   f'({n} rows). Transaction tables ของ prod ยังเหมือนเดิม.')
            if backup_path:
                msg += f' Backup: {os.path.basename(backup_path)}'
            if skipped:
                msg += f' [skipped {len(skipped)}: {[t for t,_ in skipped[:3]]}]'
            flash(msg, 'success')
            return redirect(url_for('dashboard'))

        # Full-replace mode (legacy): existing diff-check + warn flow
        diff_rows = _diff_db_row_counts(config.DATABASE_PATH, tmp)
        warnings = [d for d in diff_rows if d['warning']]
        confirmed = request.form.get('confirm') == 'yes'

        if warnings and not confirmed:
            # Hold the uploaded file in a known spot so user can confirm without re-uploading.
            hold_dir = os.path.join(os.path.dirname(config.DATABASE_PATH), 'pending_uploads')
            os.makedirs(hold_dir, exist_ok=True)
            # Stale held stashes from abandoned uploads accumulate on the small
            # Railway volume and HAVE filled it (→ a 500 on upload). Sweep them
            # first, then refuse to stash if there still isn't room — with a clear
            # message pointing at /admin/backups, NOT a disk-full crash.
            db_backup.sweep_pending_uploads(hold_dir)
            need = os.path.getsize(tmp) + 20 * 1024 * 1024
            free = shutil.disk_usage(hold_dir).free
            if free < need:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                flash(f'พื้นที่ดิสก์ไม่พอสำหรับการอัปโหลด (เหลือ {free // (1024*1024)}MB '
                      f'ต้องการ ~{need // (1024*1024)}MB) — ลบไฟล์สำรองเก่าที่หน้า "ไฟล์สำรอง" '
                      f'ก่อน แล้วลองอัปโหลดใหม่', 'danger')
                return redirect(url_for('admin.backups_list'))
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            hold_path = os.path.join(hold_dir, f'pending-{ts}.db')
            shutil.move(tmp, hold_path)
            session['pending_upload_path'] = hold_path
            return render_template(
                'admin_upload_db.html',
                diff_rows=diff_rows,
                warnings=warnings,
                pending=True,
                disk=db_backup.disk_usage_mb(os.path.dirname(config.DATABASE_PATH)),
            )

        # Always backup the current DB before replacing (WAL-safe online backup —
        # a bare file copy of a WAL-mode DB can capture a torn snapshot).
        backup_info, backup_err = db_backup.safe_create_backup(
            'pre-upload-full', db_path=config.DATABASE_PATH, backup_dir=_backups_dir())

        # Clear stale -wal/-shm BEFORE the swap (same reasoning as restore_backup:
        # under gunicorn -w 2 a connection arriving mid-swap could otherwise pair
        # the new file with old WAL frames), then again after (belt-and-braces).
        db_backup._remove_sidecars(config.DATABASE_PATH)
        shutil.move(tmp, config.DATABASE_PATH)
        db_backup._remove_sidecars(config.DATABASE_PATH)

        if backup_info:
            flash(f'อัปโหลด DB สำเร็จ. Backup เก็บไว้ที่ {backup_info["name"]}', 'success')
        elif backup_err:
            flash(f'อัปโหลด DB สำเร็จ (⚠ backup ก่อนอัปโหลดล้มเหลว: {backup_err})', 'warning')
        else:
            flash('อัปโหลด DB สำเร็จ', 'success')
        if _reload_workers_after_restore():
            flash('ระบบกำลังรีโหลดอัตโนมัติเพื่อให้ทุกตัวใช้ข้อมูลที่อัปโหลด (~ไม่กี่วินาที) '
                  'แล้วตรวจข้อมูลอีกครั้ง', 'info')
        else:
            flash('แนะนำให้ปิด-เปิดแอป (restart) แล้วตรวจข้อมูลอีกครั้ง', 'warning')
        return redirect(url_for('dashboard'))
    return render_template(
        'admin_upload_db.html',
        disk=db_backup.disk_usage_mb(os.path.dirname(config.DATABASE_PATH)),
    )


@bp_admin.route('/admin/upload-db/confirm', methods=['POST'])
def upload_db_confirm():
    """Second step after warning page: actually apply the held upload."""
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        abort(403)

    hold_path = session.pop('pending_upload_path', None)
    if not hold_path or not os.path.exists(hold_path):
        flash('ไม่พบไฟล์ที่รออัปโหลด — กรุณาอัปโหลดใหม่', 'danger')
        return redirect(url_for('admin.upload_db'))

    if request.form.get('action') == 'cancel':
        try:
            os.remove(hold_path)
        except OSError:
            pass
        flash('ยกเลิกการอัปโหลดแล้ว', 'info')
        return redirect(url_for('admin.upload_db'))

    backup_info, backup_err = db_backup.safe_create_backup(
        'pre-upload-full', db_path=config.DATABASE_PATH, backup_dir=_backups_dir())

    db_backup._remove_sidecars(config.DATABASE_PATH)
    shutil.move(hold_path, config.DATABASE_PATH)
    db_backup._remove_sidecars(config.DATABASE_PATH)

    if backup_info:
        flash(f'อัปโหลด DB สำเร็จ. Backup เก็บไว้ที่ {backup_info["name"]}', 'success')
    elif backup_err:
        flash(f'อัปโหลด DB สำเร็จ (⚠ backup ก่อนอัปโหลดล้มเหลว: {backup_err})', 'warning')
    else:
        flash('อัปโหลด DB สำเร็จ', 'success')
    if _reload_workers_after_restore():
        flash('ระบบกำลังรีโหลดอัตโนมัติเพื่อให้ทุกตัวใช้ข้อมูลที่อัปโหลด (~ไม่กี่วินาที) '
              'แล้วตรวจข้อมูลอีกครั้ง', 'info')
    else:
        flash('แนะนำให้ปิด-เปิดแอป (restart) แล้วตรวจข้อมูลอีกครั้ง', 'warning')
    return redirect(url_for('dashboard'))


# ── Auto-backup / restore (snapshots taken before every import) ───────────────
def _backups_dir():
    return db_backup.default_backup_dir(config.DATABASE_PATH)


def _reload_workers_after_restore():
    """After a DB restore, ask the gunicorn master to gracefully reload ALL
    workers (SIGHUP) so no worker keeps a connection to the pre-restore DB file.

    Railway runs `gunicorn -w 2`; get_connection() opens a fresh connection per
    request, so NEW requests already pick up the restored file — but an in-flight
    request on the sibling worker can still read pre-restore data or lose a write.
    A graceful reload guarantees both workers turn over. No-op off gunicorn (the
    Flask dev server / tests), where there is no master to signal and a single
    process self-heals on its next per-request connection. Returns True if a
    reload was signalled."""
    if 'gunicorn' not in request.environ.get('SERVER_SOFTWARE', '').lower():
        return False
    try:
        os.kill(os.getppid(), signal.SIGHUP)   # graceful reload of all workers
        return True
    except OSError:
        return False


_BACKUP_REASON_LABELS = {
    'unified': 'นำเข้า (รวมทุกไฟล์)', 'weekly': 'นำเข้ารายสัปดาห์',
    'marketplace': 'คำสั่งซื้อ Marketplace', 'pre-restore': 'ก่อนกู้คืน',
    'payments': 'นำเข้าการรับชำระ', 'credit-notes': 'นำเข้าใบลดหนี้',
    'pre-upload-full': 'ก่อนแทนที่ DB (Full replace)',
}


@bp_admin.route('/admin/backups')
def backups_list():
    if session.get('role') != 'admin':
        abort(403)
    rows = db_backup.list_backups(backup_dir=_backups_dir())
    for r in rows:
        r['reason_label'] = _BACKUP_REASON_LABELS.get(r['reason'], r['reason'])
        r['size_mb'] = round(r['size'] / (1024 * 1024), 1)
    disk = db_backup.disk_usage_mb(os.path.dirname(config.DATABASE_PATH))
    return render_template('admin_backups.html', backups=rows, disk=disk,
                           db_routes_enabled=session.get('db_routes_enabled'))


@bp_admin.route('/admin/backups/download/<name>')
def backup_download(name):
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        abort(403)
    # only serve a real auto-* snapshot from the backup dir (list_backups
    # validates the name pattern + presence; reject anything else)
    if name not in {b['name'] for b in db_backup.list_backups(backup_dir=_backups_dir())}:
        abort(404)
    return send_file(os.path.join(_backups_dir(), name),
                     as_attachment=True, download_name=name)


@bp_admin.route('/admin/backups/restore', methods=['POST'])
def backup_restore():
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        flash('เปิด "DB routes" ก่อน (ปุ่มในเมนูผู้ดูแล) เพื่อกู้คืนข้อมูล', 'danger')
        return redirect(url_for('admin.backups_list'))
    name = request.form.get('name', '')
    if request.form.get('confirm') != 'yes':
        flash('ต้องยืนยันก่อนกู้คืน', 'warning')
        return redirect(url_for('admin.backups_list'))
    try:
        db_backup.restore_backup(name, db_path=config.DATABASE_PATH,
                                 backup_dir=_backups_dir())
    except Exception as e:
        flash(f'กู้คืนไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('admin.backups_list'))
    flash(f'กู้คืนฐานข้อมูลจาก {name} สำเร็จ — ระบบสำรองสถานะก่อนกู้คืนไว้แล้ว.', 'success')
    if _reload_workers_after_restore():
        flash('ระบบกำลังรีโหลดอัตโนมัติเพื่อให้ทุกตัวใช้ข้อมูลที่กู้คืน (~ไม่กี่วินาที) '
              'แล้วตรวจข้อมูลอีกครั้ง', 'info')
    else:
        flash('แนะนำให้ปิด-เปิดแอป (restart) แล้วตรวจข้อมูลอีกครั้ง', 'warning')
    return redirect(url_for('dashboard'))


@bp_admin.route('/admin/backups/delete', methods=['POST'])
def backup_delete():
    """Manually delete one backup snapshot to free volume space. Same admin +
    db-routes gate as restore; delete_backup() rejects any non-snapshot or
    path-escaping name."""
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        flash('เปิด "DB routes" ก่อน (ปุ่มในเมนูผู้ดูแล) เพื่อลบไฟล์สำรอง', 'danger')
        return redirect(url_for('admin.backups_list'))
    name = request.form.get('name', '')
    if request.form.get('confirm') != 'yes':
        flash('ต้องยืนยันก่อนลบ', 'warning')
        return redirect(url_for('admin.backups_list'))
    try:
        freed = db_backup.delete_backup(name, backup_dir=_backups_dir())
    except Exception as e:
        flash(f'ลบไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('admin.backups_list'))
    flash(f'ลบไฟล์สำรอง {name} แล้ว — คืนพื้นที่ {freed // (1024 * 1024)}MB', 'success')
    return redirect(url_for('admin.backups_list'))
