"""Sendy ERP — Flask application module.

This is the largest file in the app (~3,600 LOC). It owns:

- Flask app construction (`app = Flask(__name__)`) + blueprint registration
- The session-based auth model: /login, /logout, _login_required, role check
- The POST permission gate (`_STAFF_POST_OK`, `_MANAGER_POST_OK`, `_before_request`)
- Most legacy routes that pre-date the blueprint split: trade dashboard,
  customers, suppliers, BSN import / mapping / unit-conversions, payment
  status, ecommerce, conversions (manufacturing), commission UI, express
  AR/AP, labels, admin DB upload/download
- The module-switcher sidebar metadata (`_MODULES`)

Domain-coherent areas have been extracted to blueprints (see
`inventory_app/blueprints/`): products, cashbook, hr, supplier_catalogue,
mobile. Future splits — inventory, bsn, sales, payments, ecommerce, admin —
are opportunistic; do one when a natural touchpoint brings you here.

Permission model (see `_STAFF_POST_OK` / `_MANAGER_POST_OK` near the top):
  - admin: full access + user management
  - manager: see cost/GP/payments; cannot edit products/users
  - staff: import weekly flow + read-only views (no cost/GP, no hr.*,
    no cashbook.*, no supplier_catalogue.*)
"""
import io
import json
import os
import signal
import sys
import sqlite3
import shutil
import tempfile
import time

# Force the process timezone to Thailand (UTC+7) before anything reads a clock.
# Railway/nixpacks containers default to UTC, which makes datetime.now(),
# date.today() and SQLite datetime('now','localtime') read 7h behind Bangkok
# (wrong calendar day between 00:00-07:00 local). Use the POSIX form, NOT
# TZ='Asia/Bangkok': on this image TZDIR is unset so glibc cannot resolve the
# named zone and silently falls back to UTC. Thailand has no DST and is fixed
# at +7, so a static offset is exact and never needs adjustment. setdefault
# lets an explicit TZ env var still win (e.g. for local testing).
os.environ.setdefault('TZ', 'ICT-7')
time.tzset()

from datetime import date, datetime

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, abort, send_file,
                   send_from_directory, Response)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import models
import hr_queries as hrq
import db_backup
from database import init_db, get_connection
from parse_platform import (parse_shopee, parse_lazada, export_shopee, export_lazada,
                            export_mapping, parse_mapping,
                            parse_shopee_orders, parse_lazada_orders,
                            export_listing_mapping, parse_listing_mapping)
from blueprints.products import bp_products
from blueprints.supplier_catalogue import bp_supplier_catalogue
from blueprints.mobile import bp_mobile
from blueprints.hr import bp_hr
from blueprints.cashbook import bp_cashbook
from blueprints.marketplace import bp_marketplace
from blueprints.review import bp_review
from blueprints.call import bp_call
from blueprints.customer_review import bp_customer_review
from blueprints.naming import bp_naming
from blueprints.me import bp_me
from blueprints.bsn import bp_bsn
from blueprints.inventory import bp_inventory
from blueprints.partners import bp_partners
from blueprints.sales import bp_sales
from blueprints.commission_bp import bp_commission
import cashflow as cf_mod
import revenue as rev_mod
import ar_followup as arf_mod
import payments_alloc as pa_mod
# Re-exported below for app.py's own remaining code (_role_home, ROLES) and
# for tests that import these off `app` (_STAFF_POST_OK, _MANAGER_POST_OK,
# _ENDPOINT_MODULE, _MODULE_DEFS, build_mobile_nav_slots).
from access_control import (_STAFF_POST_OK, _MANAGER_POST_OK, _ENDPOINT_MODULE,
                            _MODULE_DEFS, ROLES, ROLE_ORDER, _role_home,
                            build_mobile_nav_slots, init_access_control)
from filters import register_filters

app = Flask(__name__)
# Honor X-Forwarded-Proto/Host from Railway's edge so url_for and post-login
# redirects use https instead of http. Trust exactly one proxy hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['JSON_AS_ASCII'] = False
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['ITEMS_PER_PAGE'] = config.ITEMS_PER_PAGE
# Upload/Download DB unlock is stored per-session (see toggle_db_routes), NOT
# in a process-global app.config flag. app.config is per gunicorn worker, so a
# per-worker flag armed only one worker and the routes 403'd ~50% of the time
# on Railway (-w 2). The signed-cookie session travels with every request.
app.config['PERMANENT_SESSION_LIFETIME'] = config.PERMANENT_SESSION_LIFETIME
app.config['SESSION_COOKIE_HTTPONLY']    = config.SESSION_COOKIE_HTTPONLY
app.config['SESSION_COOKIE_SAMESITE']    = config.SESSION_COOKIE_SAMESITE
app.config['SESSION_COOKIE_SECURE']      = config.SESSION_COOKIE_SECURE

# CSRF protection. Production default = on. Tests set WTF_CSRF_ENABLED=False
# via env (tests/conftest.py) so the existing POST tests don't need rewriting;
# tests/test_csrf_protection.py re-enables it per-fixture to assert the gate works.
app.config['WTF_CSRF_ENABLED'] = (
    os.environ.get('WTF_CSRF_ENABLED', 'True').lower() not in ('false', '0', 'no')
)
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def _csrf_error(e):
    flash(f'เซสชันหมดอายุ กรุณารีเฟรชหน้าและลองอีกครั้ง ({e.description})', 'danger')
    return redirect(request.referrer or url_for('dashboard'))


os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)

app.register_blueprint(bp_products)
app.register_blueprint(bp_supplier_catalogue)
app.register_blueprint(bp_mobile)
app.register_blueprint(bp_hr)
app.register_blueprint(bp_cashbook)
app.register_blueprint(bp_marketplace)
app.register_blueprint(bp_review)
app.register_blueprint(bp_call)
app.register_blueprint(bp_customer_review)
app.register_blueprint(bp_naming)
app.register_blueprint(bp_me)
app.register_blueprint(bp_bsn)
app.register_blueprint(bp_inventory)
app.register_blueprint(bp_partners)
app.register_blueprint(bp_sales)
app.register_blueprint(bp_commission)

with app.app_context():
    # SKIP_DB_INIT=1 lets the app boot without touching the database. Used
    # one-shot during the Railway DB upload bootstrap (volume is empty, so
    # init_db() would crash on the migration runner). Unset after the volume
    # has been seeded with the real DB.
    if os.environ.get('SKIP_DB_INIT', '').lower() not in ('1', 'true', 'yes'):
        init_db()


# Liveness probe that does not touch the database. Used as the Railway
# healthcheck path during the SKIP_DB_INIT bootstrap window; safe to leave
# in place afterwards.
@app.route('/healthz')
def healthz():
    return 'ok', 200


# PWA Service Worker — served from root so its scope covers the whole app.
# A SW at /static/sw.js would only control /static/*, which is useless.
# No auth required: the browser fetches this outside any user session.
@app.route('/sw.js')
def serve_sw():
    return send_from_directory(app.static_folder, 'sw.js',
                               mimetype='application/javascript')


# PWA install instructions — Thai step-by-step for Android Chrome + iPhone Safari.
# No auth required: reachable from the install prompt before login.
@app.route('/help/install')
def help_install():
    return render_template('help/install.html')


# Bootstrap-only DB upload. Separate from the admin /admin/upload-db route
# (which needs an admin session + populated DB to work). This one is active
# only when BOOTSTRAP_TOKEN env var is set, gated by token instead of session,
# and never renders a template — so it works on a fresh empty volume. Unset
# BOOTSTRAP_TOKEN after the seed to disable the endpoint.
@app.route('/bootstrap/upload-db', methods=['GET', 'POST'])
@csrf.exempt
def bootstrap_upload_db():
    expected = os.environ.get('BOOTSTRAP_TOKEN', '')
    if not expected:
        abort(404)
    if request.method == 'POST':
        if request.form.get('token', '') != expected:
            return 'bad token', 403
        f = request.files.get('db')
        if not f:
            return 'missing file field "db"', 400
        target = config.DATABASE_PATH
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Stream to a temp file then atomic-rename so a partial upload doesn't
        # leave the volume with a half-written DB.
        tmp = target + '.upload-tmp'
        f.save(tmp)
        size = os.path.getsize(tmp)
        os.replace(tmp, target)
        return f'ok — wrote {size:,} bytes to {target}\n', 200
    return (
        '<!doctype html><meta charset=utf-8>'
        '<title>Sendy bootstrap upload</title>'
        '<form method=post enctype=multipart/form-data>'
        '<p><label>Token: <input type=password name=token required></label></p>'
        '<p><label>DB file: <input type=file name=db accept=".db" required></label></p>'
        '<p><button type=submit>Upload</button></p>'
        '</form>'
    )


# ── Auth + template filters ──────────────────────────────────────────────────────
# Moved to access_control.py (role/permission constants, before_request
# gate, context processor) and filters.py (Jinja template filters).
# Registered here so their effects take hold in the same relative order
# as before (after CSRF setup + blueprint registration).
init_access_control(app)
register_filters(app)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND is_active=1", (username,)
        ).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            remember = request.form.get('remember') == '1'
            session.clear()
            session['user_id']      = user['id']
            session['username']     = user['username']
            session['display_name'] = user['display_name'] or user['username']
            session['role']         = user['role']
            session.permanent       = remember   # 30-day cookie when checked
            flash(f'ยินดีต้อนรับ {session["display_name"]}', 'success')
            return redirect(request.args.get('next') or _role_home(session['role']))
        flash('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง', 'danger')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('ออกจากระบบแล้ว', 'success')
    return redirect(url_for('dashboard'))


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


@app.route('/users')
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


@app.route('/users/new', methods=['POST'])
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
        return redirect(url_for('user_list'))
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
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/edit', methods=['POST'])
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
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/delete', methods=['POST'])
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
    return redirect(url_for('user_list'))


@app.route('/admin/simulate-role', methods=['POST'])
def admin_simulate_role():
    # Impersonate a specific USER: become them (user_id+role+display_name) so
    # identity-keyed pages (/me/*) show THEIR data and act-as is attributed to
    # them. Only a real admin may start; while already impersonating, _real_role
    # is set (current role is the impersonated one) so switching user is allowed.
    if session.get('role') != 'admin' and not session.get('_real_role'):
        abort(403)
    try:
        target_id = int(request.form.get('user_id', ''))
    except (TypeError, ValueError):
        target_id = None
    conn = get_connection()
    target = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id=?", (target_id,)
    ).fetchone() if target_id else None
    conn.close()
    if not target or target['role'] == 'admin':
        flash('เลือกผู้ใช้ไม่ถูกต้อง', 'danger')
        return redirect(url_for('user_list'))
    # Stash the REAL identity once, so user→user switches keep the original admin.
    if not session.get('_real_role'):
        session['_real_role']         = session.get('role')
        session['_real_user_id']      = session.get('user_id')
        session['_real_username']     = session.get('username')
        session['_real_display_name'] = session.get('display_name')
    # Trail for any act-as write: log the enter event under the REAL admin.
    # (Not audit_log — its CHECK limits action to INSERT/UPDATE/DELETE, and
    # impersonation is not a row mutation.)
    app.logger.info("IMPERSONATE enter: %s -> user %s (%s, role=%s)",
                    session.get('_real_username') or session.get('username'),
                    target['id'], target['username'], target['role'])
    # Become the target user.
    session['user_id']      = target['id']
    session['username']     = target['username']
    session['display_name'] = target['display_name'] or target['username']
    session['role']         = target['role']
    flash(f'กำลังดูในมุมมองของ {session["display_name"]} — คลิก "ออกจากโหมดจำลอง" เพื่อกลับ', 'info')
    return redirect(_role_home(target['role']))


@app.route('/admin/exit-simulate', methods=['POST'])
def admin_exit_simulate():
    real_role = session.pop('_real_role', None)
    if real_role:
        real_admin = session.get('_real_username') or session.get('username')
        session['role']         = real_role
        session['user_id']      = session.pop('_real_user_id', session.get('user_id'))
        session['username']     = session.pop('_real_username', session.get('username'))
        session['display_name'] = session.pop('_real_display_name', session.get('display_name'))
        app.logger.info("IMPERSONATE exit: back to %s", real_admin)
        flash('ออกจากโหมดจำลองแล้ว กลับเป็น Admin', 'success')
    return redirect(url_for('dashboard'))


# ── Temp: Download DB (ลบออกหลังใช้) ─────────────────────────────────────────

@app.route('/admin/toggle-db-routes', methods=['POST'])
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


@app.route('/admin/download-db')
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


def _diff_master_tables(current_path, uploaded_path):
    """Preview row counts for MASTER tables only — used in master-only mode.
    No 'warning' flag because master-only doesn't risk transaction data loss."""
    rows = []
    for table in _MASTER_TABLES:
        cur = _count_rows(current_path, table)
        upl = _count_rows(uploaded_path, table)
        if cur is None and upl is None:
            continue
        rows.append({
            'table':    table,
            'current':  cur if cur is not None else 0,
            'uploaded': upl if upl is not None else 0,
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


@app.route('/admin/upload-db', methods=['GET', 'POST'])
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
                return redirect(url_for('upload_db'))

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
                return redirect(url_for('backups_list'))
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


@app.route('/admin/upload-db/confirm', methods=['POST'])
def upload_db_confirm():
    """Second step after warning page: actually apply the held upload."""
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        abort(403)

    hold_path = session.pop('pending_upload_path', None)
    if not hold_path or not os.path.exists(hold_path):
        flash('ไม่พบไฟล์ที่รออัปโหลด — กรุณาอัปโหลดใหม่', 'danger')
        return redirect(url_for('upload_db'))

    if request.form.get('action') == 'cancel':
        try:
            os.remove(hold_path)
        except OSError:
            pass
        flash('ยกเลิกการอัปโหลดแล้ว', 'info')
        return redirect(url_for('upload_db'))

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


@app.route('/admin/backups')
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


@app.route('/admin/backups/download/<name>')
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


@app.route('/admin/backups/restore', methods=['POST'])
def backup_restore():
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        flash('เปิด "DB routes" ก่อน (ปุ่มในเมนูผู้ดูแล) เพื่อกู้คืนข้อมูล', 'danger')
        return redirect(url_for('backups_list'))
    name = request.form.get('name', '')
    if request.form.get('confirm') != 'yes':
        flash('ต้องยืนยันก่อนกู้คืน', 'warning')
        return redirect(url_for('backups_list'))
    try:
        db_backup.restore_backup(name, db_path=config.DATABASE_PATH,
                                 backup_dir=_backups_dir())
    except Exception as e:
        flash(f'กู้คืนไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('backups_list'))
    flash(f'กู้คืนฐานข้อมูลจาก {name} สำเร็จ — ระบบสำรองสถานะก่อนกู้คืนไว้แล้ว.', 'success')
    if _reload_workers_after_restore():
        flash('ระบบกำลังรีโหลดอัตโนมัติเพื่อให้ทุกตัวใช้ข้อมูลที่กู้คืน (~ไม่กี่วินาที) '
              'แล้วตรวจข้อมูลอีกครั้ง', 'info')
    else:
        flash('แนะนำให้ปิด-เปิดแอป (restart) แล้วตรวจข้อมูลอีกครั้ง', 'warning')
    return redirect(url_for('dashboard'))


@app.route('/admin/backups/delete', methods=['POST'])
def backup_delete():
    """Manually delete one backup snapshot to free volume space. Same admin +
    db-routes gate as restore; delete_backup() rejects any non-snapshot or
    path-escaping name."""
    if session.get('role') != 'admin':
        abort(403)
    if not session.get('db_routes_enabled'):
        flash('เปิด "DB routes" ก่อน (ปุ่มในเมนูผู้ดูแล) เพื่อลบไฟล์สำรอง', 'danger')
        return redirect(url_for('backups_list'))
    name = request.form.get('name', '')
    if request.form.get('confirm') != 'yes':
        flash('ต้องยืนยันก่อนลบ', 'warning')
        return redirect(url_for('backups_list'))
    try:
        freed = db_backup.delete_backup(name, backup_dir=_backups_dir())
    except Exception as e:
        flash(f'ลบไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('backups_list'))
    flash(f'ลบไฟล์สำรอง {name} แล้ว — คืนพื้นที่ {freed // (1024 * 1024)}MB', 'success')
    return redirect(url_for('backups_list'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    restock_count = models.count_restock_needed()
    recent_txns = models.get_recent_transactions(10)
    total_products = models.count_active_products()
    in_stock_count = models.count_in_stock()
    payroll_reminder = None
    if session.get('role') == 'admin':
        conn = get_connection()
        try:
            payroll_reminder = hr_mod.payroll_reminder_month(date.today(), conn)
        finally:
            conn.close()
    return render_template('dashboard.html',
                           restock_count=restock_count,
                           recent_txns=recent_txns,
                           total_products=total_products,
                           in_stock_count=in_stock_count,
                           payroll_reminder=payroll_reminder)


# ── Products — moved to blueprints/products.py ────────────────────────────────
# Routes: /products, /products/new, /products/<id>, /products/<id>/cost-history,
#         /products/<id>/pricing, /products/<id>/edit, /products/<id>/location,
#         /products/<id>/online-stock, /products/<id>/deactivate,
#         /products/<id>/trade, /products/<id>/promotions/new,
#         /promotions/<id>/deactivate, /import, /import/confirm
# (registered via bp_products above)


# ── Promotions and CSV Import — moved to blueprints/products.py ───────────────


# ── Sales View, Sales/Purchases Doc, Payment Status — moved to blueprints/sales.py ──

# ── Customers, Suppliers — moved to blueprints/partners.py ──────────────────


# ── E-commerce ────────────────────────────────────────────────────────────────

@app.route('/ecommerce')
def ecommerce():
    tab      = request.args.get('tab', 'shopee')
    search   = request.args.get('q', '').strip()
    page     = int(request.args.get('page', 1))
    per_page = app.config['ITEMS_PER_PAGE']

    listing_summary = models.get_ecommerce_listing_summary()

    if tab == 'mapping':
        mapped_filter = request.args.get('mapped')
        mapped = True if mapped_filter == '1' else (False if mapped_filter == '0' else None)
        platform_filter = request.args.get('platform')
        rows, total = models.get_ecommerce_listings(
            platform=platform_filter or None,
            search=search or None,
            mapped=mapped,
            page=page,
            per_page=per_page,
        )
        pages   = max(1, (total + per_page - 1) // per_page)
        summary = models.get_platform_summary()
        return render_template('ecommerce.html',
                               tab=tab, rows=rows, total=total,
                               search=search, page=page, pages=pages,
                               summary=summary, listing_summary=listing_summary,
                               mapped_filter=mapped_filter, platform_filter=platform_filter or '')

    platform = tab if tab in ('shopee', 'lazada') else 'shopee'
    rows, total = models.get_platform_skus(platform, search or None, page, per_page)
    pages   = max(1, (total + per_page - 1) // per_page)
    summary = models.get_platform_summary()

    return render_template('ecommerce.html',
                           tab=tab, rows=rows, total=total,
                           search=search, page=page, pages=pages,
                           summary=summary, listing_summary=listing_summary,
                           mapped_filter=None, platform_filter='')


@app.route('/ecommerce/import', methods=['POST'])
def ecommerce_import():
    platform = request.form.get('platform', '').lower()
    if platform not in ('shopee', 'lazada'):
        flash('ระบุ platform ไม่ถูกต้อง', 'danger')
        return redirect(url_for('ecommerce'))

    f = request.files.get('platform_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce', tab=platform))

    try:
        file_bytes = io.BytesIO(f.read())
        if platform == 'shopee':
            records = parse_shopee(file_bytes)
        else:
            records = parse_lazada(file_bytes)

        if not records:
            flash('ไม่พบข้อมูลในไฟล์', 'warning')
            return redirect(url_for('ecommerce', tab=platform))

        count, propagated = models.import_platform_skus(platform, records)
        flash(f'นำเข้าข้อมูล {platform.capitalize()} สำเร็จ {count} รายการ '
              f'(restore mapping {propagated} รายการ จาก ecommerce_listings)',
              'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')

    return redirect(url_for('ecommerce', tab=platform))


@app.route('/ecommerce/export/<platform>')
def ecommerce_export(platform):
    if platform not in ('shopee', 'lazada'):
        abort(404)

    rows = models.get_platform_skus_all(platform)
    if not rows:
        flash(f'ยังไม่มีข้อมูล {platform.capitalize()} ในระบบ', 'warning')
        return redirect(url_for('ecommerce', tab=platform))

    from flask import send_file
    import datetime
    date_str = datetime.date.today().strftime('%Y%m%d')

    if platform == 'shopee':
        buf = export_shopee([dict(r) for r in rows])
        fname = f'Shopee_mass_update_{date_str}.xlsx'
        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    else:
        buf = export_lazada([dict(r) for r in rows])
        fname = f'Lazada_pricestock_{date_str}.xlsx'
        mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    return send_file(buf, mimetype=mimetype,
                     as_attachment=True, download_name=fname)


@app.route('/ecommerce/mapping/export')
def ecommerce_mapping_export():
    rows = models.get_platform_mapping_data()
    if not rows:
        flash('ยังไม่มีข้อมูล platform ในระบบ', 'warning')
        return redirect(url_for('ecommerce'))

    from flask import send_file
    import datetime

    # Compute AI suggestions (~6s)
    suggestions = models.suggest_platform_mapping()

    buf = export_mapping(rows, suggestions=suggestions)
    fname = f'ecommerce_mapping_{datetime.date.today().strftime("%Y%m%d")}.xlsx'

    # บันทึกลง data/exports/ ด้วยทุกครั้ง
    exports_dir = os.path.join(os.path.dirname(config.BASE_DIR), 'data', 'exports')
    os.makedirs(exports_dir, exist_ok=True)
    save_path = os.path.join(exports_dir, fname)
    with open(save_path, 'wb') as f:
        f.write(buf.getvalue())
    buf.seek(0)

    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


@app.route('/ecommerce/mapping/import', methods=['POST'])
def ecommerce_mapping_import():
    f = request.files.get('mapping_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce'))

    try:
        file_bytes = io.BytesIO(f.read())
        records = parse_mapping(file_bytes)
        updated, not_found = models.apply_platform_mapping(records)
        flash(f'Mapping สำเร็จ {updated} รายการ'
              + (f' | ไม่พบ SKU ในระบบ {not_found} รายการ' if not_found else ''),
              'success' if not_found == 0 else 'warning')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')

    return redirect(url_for('ecommerce'))


@app.route('/ecommerce/sku/<int:sku_id>/edit', methods=['POST'])
def ecommerce_sku_edit(sku_id):
    platform = request.form.get('platform', 'shopee')
    try:
        models.update_platform_sku(
            sku_id,
            price       = float(request.form['price']) if request.form.get('price') else None,
            special_price = float(request.form['special_price']) if request.form.get('special_price') else None,
            stock       = int(request.form['stock']) if request.form.get('stock') else None,
            qty_per_sale = float(request.form.get('qty_per_sale') or 1),
        )
        flash('อัปเดตเรียบร้อย', 'success')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')
    return redirect(url_for('ecommerce', tab=platform,
                            page=request.form.get('page', 1),
                            q=request.form.get('q', '')))



# ── Ecommerce Listing Mapping ─────────────────────────────────────────────────

@app.route('/ecommerce/listings/import', methods=['POST'])
def ecommerce_listings_import():
    platform = request.form.get('platform', '').lower()
    if platform not in ('shopee', 'lazada'):
        flash('ระบุ platform ไม่ถูกต้อง', 'danger')
        return redirect(url_for('ecommerce', tab='mapping'))

    files = request.files.getlist('order_files')
    if not files or all(not f.filename for f in files):
        flash('กรุณาเลือกไฟล์', 'danger')
        return redirect(url_for('ecommerce', tab='mapping'))

    total_added = total_skipped = 0
    errors = []
    for f in files:
        if not f.filename.endswith('.xlsx'):
            errors.append(f'{f.filename}: ต้องเป็นไฟล์ .xlsx')
            continue
        try:
            file_bytes = io.BytesIO(f.read())
            if platform == 'shopee':
                records = parse_shopee_orders(file_bytes)
            else:
                records = parse_lazada_orders(file_bytes)
            added, skipped = models.import_ecommerce_listings(records)
            total_added   += added
            total_skipped += skipped
        except Exception as e:
            errors.append(f'{f.filename}: {e}')

    if errors:
        flash(' | '.join(errors), 'danger')
    if total_added or total_skipped:
        flash(f'นำเข้า {platform.capitalize()} สำเร็จ: เพิ่มใหม่ {total_added} รายการ, ซ้ำข้าม {total_skipped} รายการ', 'success')
    return redirect(url_for('ecommerce', tab='mapping'))


@app.route('/ecommerce/listings/mapping-export')
def ecommerce_listings_mapping_export():
    unmatched_only = request.args.get('unmatched') == '1'
    rows = models.get_listing_mapping_data(unmatched_only=unmatched_only)
    if not rows:
        flash('ยังไม่มีข้อมูล listing ในระบบ', 'warning')
        return redirect(url_for('ecommerce', tab='mapping'))

    from flask import send_file
    import datetime
    suggestions = models.suggest_listing_mapping()
    buf = export_listing_mapping(rows, suggestions=suggestions, unmatched_only=False)
    suffix = '_unmatched' if unmatched_only else ''
    fname  = f'ecommerce_listing_mapping{suffix}_{datetime.date.today().strftime("%Y%m%d")}.xlsx'

    exports_dir = os.path.join(os.path.dirname(config.BASE_DIR), 'data', 'exports')
    os.makedirs(exports_dir, exist_ok=True)
    with open(os.path.join(exports_dir, fname), 'wb') as fh:
        fh.write(buf.getvalue())
    buf.seek(0)

    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


@app.route('/ecommerce/listings/mapping-import', methods=['POST'])
def ecommerce_listings_mapping_import():
    f = request.files.get('listing_mapping_file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('กรุณาเลือกไฟล์ .xlsx', 'danger')
        return redirect(url_for('ecommerce', tab='mapping'))
    try:
        file_bytes = io.BytesIO(f.read())
        records = parse_listing_mapping(file_bytes)
        updated, not_found = models.apply_listing_mapping(records)
        flash(f'Mapping สำเร็จ {updated} รายการ'
              + (f' | ไม่พบ SKU ในระบบ {not_found} รายการ' if not_found else ''),
              'success' if not_found == 0 else 'warning')
    except Exception as e:
        flash(f'เกิดข้อผิดพลาด: {e}', 'danger')
    return redirect(url_for('ecommerce', tab='mapping'))


# ── Customer Map — moved to blueprints/partners.py ───────────────────────────


# ── Commission dashboard, payouts, drilldown, export, overrides — moved to
#    blueprints/commission_bp.py ────────────────────────────────────────────
import hr as hr_mod  # noqa: E402  (referenced by blueprints/hr.py via direct import)


# ── Regions admin — moved to blueprints/partners.py ──────────────────────────


@app.route('/express/import')
def express_import():
    # Legacy single-file Express uploader (AR/AP snapshot, payments-out, credit
    # notes). Superseded by the unified box (/import-data), which auto-detects
    # and routes every Express report type. Kept as a redirect for old links.
    return redirect(url_for('bsn.unified_import'))


@app.route('/express/ar')
def express_ar_dashboard():
    """Redirect stub — content moved to /ar?tab=overview (AR consolidation)."""
    return redirect(url_for('ar_dashboard', tab='overview'))


@app.route('/express/ar/customer/<customer_code>')
def express_ar_customer(customer_code):
    """Per-customer AR drill-down — all unpaid invoices in the latest snapshot."""
    conn = get_connection()
    snapshot = conn.execute(
        "SELECT MAX(snapshot_date_iso) AS d FROM express_ar_outstanding WHERE entity = 'BSN'"
    ).fetchone()
    snapshot_date = snapshot['d'] if snapshot else None

    rows = conn.execute("""
        SELECT customer_code, customer_name, customer_type, salesperson_code,
               doc_no, doc_date_iso, bill_amount, paid_amount, outstanding_amount,
               is_anomalous, has_warning,
               CAST(julianday('now') - julianday(doc_date_iso) AS INTEGER) AS age_days
          FROM express_ar_outstanding
         WHERE entity = 'BSN'
           AND snapshot_date_iso = ?
           AND customer_code = ?
           AND is_anomalous = 0
         ORDER BY doc_date_iso ASC
    """, (snapshot_date, customer_code)).fetchall()

    if not rows:
        flash(f'ไม่พบลูกหนี้รหัส {customer_code}', 'warning')
        return redirect(url_for('express_ar_dashboard'))

    customer_name = rows[0]['customer_name']
    customer_type = rows[0]['customer_type']
    salesperson_code = rows[0]['salesperson_code']
    total_outstanding = sum((r['outstanding_amount'] or 0) for r in rows)
    total_billed = sum((r['bill_amount'] or 0) for r in rows)
    oldest = min((r['doc_date_iso'] or '9999-12-31') for r in rows)

    # Pull recent payment history from the CANONICAL received_payments table
    # (the express_payments_in twin is frozen / being retired). received_payments
    # has no customer_code FK, so match by name; it carries a single `total`
    # rather than a cash/cheque/discount split.
    recent_payments = conn.execute("""
        SELECT rp.re_no       AS doc_no,
               rp.date_iso,
               rp.total,
               rp.salesperson AS salesperson_code
          FROM received_payments rp
         WHERE rp.cancelled = 0
           AND rp.customer = ?
         ORDER BY rp.date_iso DESC
         LIMIT 20
    """, (customer_name,)).fetchall()
    conn.close()

    return render_template('express_ar_customer.html',
                           customer_code=customer_code,
                           customer_name=customer_name,
                           customer_type=customer_type,
                           salesperson_code=salesperson_code,
                           snapshot_date=snapshot_date,
                           rows=[dict(r) for r in rows],
                           recent_payments=[dict(r) for r in recent_payments],
                           total_outstanding=total_outstanding,
                           total_billed=total_billed,
                           oldest_date=oldest)


@app.route('/express/ap')
def express_ap_dashboard():
    """Redirect stub — keep bookmarks working."""
    return redirect(url_for('ap_dashboard', tab='overview'))


# ── Unified AP page ───────────────────────────────────────────────────────────

@app.route('/ap')
def ap_dashboard():
    """Unified payables page. Tabs: overview | suppliers | payments.
    VIEW open to any logged-in role (read-only; payments come from imports)."""
    tab = request.args.get('tab', 'overview')
    date_from = request.args.get('from') or '2024-01-01'
    date_to   = request.args.get('to')   or date.today().isoformat()
    conn = get_connection()
    ap = models.get_ap_outstanding(conn)
    summary = conn.execute("""
        SELECT COUNT(*) AS n_payments, COUNT(DISTINCT supplier_name) AS n_suppliers,
               ROUND(SUM(cash_amount + cheque_amount), 2) AS total_paid
          FROM express_payments_out
         WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
    """, (date_from, date_to)).fetchone()
    ctx = {'tab': tab, 'ap': ap, 'summary': dict(summary) if summary else {},
           'date_from': date_from, 'date_to': date_to}

    if tab in ('suppliers', 'payments'):
        ctx['pay_rows'] = [dict(r) for r in conn.execute("""
            SELECT supplier_name, COUNT(*) AS payments,
                   ROUND(SUM(invoice_amount), 2) AS invoice_total,
                   ROUND(SUM(cash_amount + cheque_amount), 2) AS paid_total,
                   ROUND(SUM(discount_amount), 2) AS discount_total,
                   MAX(date_iso) AS last_paid
              FROM express_payments_out
             WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
             GROUP BY supplier_name ORDER BY paid_total DESC
        """, (date_from, date_to)).fetchall()]

    if tab == 'suppliers':
        owed = {s['supplier_name']: s['subtotal'] for s in ap['suppliers']}
        paid = {p['supplier_name']: p for p in ctx['pay_rows']}
        names = list(owed) + [n for n in paid if n not in owed]
        ctx['supplier_rows'] = sorted(
            [{'supplier_name': n, 'owed': owed.get(n, 0.0),
              'paid': (paid.get(n) or {}).get('paid_total', 0.0),
              'last_paid': (paid.get(n) or {}).get('last_paid')} for n in names],
            key=lambda r: r['owed'], reverse=True)

    if tab == 'payments':
        ctx['recent'] = [dict(r) for r in conn.execute("""
            SELECT doc_no, date_iso, supplier_name, invoice_amount,
                   (cash_amount + cheque_amount) AS paid, note
              FROM express_payments_out
             WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
             ORDER BY date_iso DESC, doc_no DESC LIMIT 50
        """, (date_from, date_to)).fetchall()]

    conn.close()
    return render_template('ap.html', **ctx)


# ── Accounting Summary ────────────────────────────────────────────────────────

@app.route('/accounting')
def accounting_summary():
    """
    Accounting summary landing page for the 'การค้า & บัญชี' module.
    Admin + manager: full view including cost/margin.
    Staff: redirected — same gating as cost-visible pages (e.g. partners.customer_summary).
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    date_from = request.args.get('date_from') or None
    date_to = request.args.get('date_to') or None
    year_month = request.args.get('month') or None  # YYYY-MM shortcut

    # If a YYYY-MM shortcut is given, derive date_from/date_to from it
    if year_month and not date_from and not date_to:
        import calendar as _cal
        try:
            y, m = int(year_month[:4]), int(year_month[5:7])
            date_from = f'{y:04d}-{m:02d}-01'
            date_to = f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            year_month = None

    summary = models.get_accounting_summary(date_from, date_to)
    return render_template('accounting.html', s=summary)


# ── Unified AR page ───────────────────────────────────────────────────────────

@app.route('/ar')
def ar_dashboard():
    """Unified receivables page. Tabs: overview | customers | invoices | reconcile.
    VIEW open to any logged-in role (staff incl.); dunning WRITES stay manager+."""
    tab = request.args.get('tab', 'overview')
    is_ar_manager = session.get('role') in ('admin', 'manager')
    is_ar_admin = session.get('role') == 'admin'   # dunning log writes are admin-only
    ctx = {'tab': tab,
           'snapshot_date': cf_mod.ar_aging().get('as_of'),
           'is_ar_manager': is_ar_manager,
           'is_ar_admin': is_ar_admin}

    if tab == 'overview':
        debt = models.get_customer_debt_summary()
        summ = models.get_payment_summary()
        snapshot_total = sum(r['outstanding_amount'] or 0 for r in debt)
        ledger_unpaid = summ['unpaid_amount']
        diff_amount = ledger_unpaid - snapshot_total
        ctx.update(
            snapshot_total=snapshot_total,
            ledger_unpaid=ledger_unpaid,
            unpaid_count=summ['unpaid_count'],
            diff_amount=diff_amount,
            aging=cf_mod.ar_aging(),
            top_customers=debt[:8],
        )
    elif tab == 'customers':
        bucket = request.args.get('bucket', '').strip()
        min_str = request.args.get('min', '').strip()
        search = request.args.get('q', '').strip()
        sort = request.args.get('sort', 'outstanding')
        try:
            min_amt = float(min_str.replace(',', '')) if min_str else 0.0
        except ValueError:
            min_amt = 0.0
        # customer_ranking() has per-customer age_buckets + oldest_age_days for filters/display
        all_ranked = arf_mod.customer_ranking(min_outstanding=min_amt)
        if bucket in ('0-30', '31-60', '61-90', '90+'):
            all_ranked = [r for r in all_ranked if r['age_buckets'].get(bucket, 0) > 0]
        if search:
            s = search.lower()
            all_ranked = [r for r in all_ranked
                          if s in (r['customer'] or '').lower()
                          or s in (r.get('customer_code') or '').lower()]
        if sort == 'age':
            all_ranked.sort(key=lambda r: -r['oldest_age_days'])
        elif sort == 'count':
            all_ranked.sort(key=lambda r: -r['invoice_count'])
        # else already sorted by outstanding DESC from customer_ranking
        ctx.update(
            customer_rows=all_ranked,
            bucket=bucket,
            min_str=min_str,
            search=search,
            sort=sort,
            customer_total=sum(r['outstanding'] or 0 for r in all_ranked),
        )
    elif tab == 'invoices':
        inv_status = request.args.get('status', 'all')
        inv_search = request.args.get('q', '').strip()
        date_from = request.args.get('date_from', '').strip()
        date_to = request.args.get('date_to', '').strip()
        page = int(request.args.get('page', 1))
        per_page = app.config['ITEMS_PER_PAGE']
        rows, total = models.get_payment_status(
            status=inv_status, search=inv_search,
            date_from=date_from, date_to=date_to,
            page=page, per_page=per_page,
        )
        summ = models.get_payment_summary()
        total_pages = max(1, (total + per_page - 1) // per_page)
        ctx.update(
            inv_rows=rows, inv_total=total,
            summary=summ,
            inv_status=inv_status, inv_search=inv_search,
            date_from=date_from, date_to=date_to,
            page=page, total_pages=total_pages,
        )
    elif tab == 'reconcile':
        rec = models.get_ar_reconciliation()
        ctx['reconcile'] = rec

    return render_template('ar.html', **ctx)


# ── Cash Flow Dashboard ────────────────────────────────────────────────────────

@app.route('/cashflow')
def cashflow_dashboard():
    """Cash flow dashboard: cash-in by RE month + AR aging + accrual revenue.

    Admin + manager only (same gating as accounting_summary).
    Optional ?from=YYYY-MM&to=YYYY-MM period filter.
    Default: last 12 months ending the latest data month.
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None

    # Derive date_from / date_to from YYYY-MM shortcuts
    def _month_start(ym):
        """'YYYY-MM' → 'YYYY-MM-01'"""
        return ym + '-01'

    def _month_end(ym):
        """'YYYY-MM' → last day of that month"""
        import calendar as _cal
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    # Default: last 12 calendar months ending today's month (inclusive).
    # Subtract 11 from (year*12 + month-1) to land on the same month one year ago + 1.
    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    date_from = _month_start(from_month)
    date_to   = _month_end(to_month)

    cash_rows   = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    aging       = cf_mod.ar_aging()          # always point-in-time today
    revenue_rows = cf_mod.revenue_by_month(date_from=date_from, date_to=date_to)

    total_cash_in     = round(sum(r['cash_in'] for r in cash_rows), 2)
    total_receipts    = sum(r['receipts'] for r in cash_rows)
    total_outstanding = aging['total_outstanding']
    total_open_count  = sum(b['count'] for b in aging['buckets'])

    # Customer-credit-balance section (point-in-time today, not period).
    # Single snapshot then Python-filter — avoids double-querying and the
    # drift that two separate calls could produce in a concurrent import.
    show_all_credit  = request.args.get('show_all') in ('1', 'true', 'on')
    credit_threshold = 0.0 if show_all_credit else 5.0
    all_credit_rows  = pa_mod.customer_credit_rows(threshold=0.0)
    credit_rows = (all_credit_rows if show_all_credit
                   else [r for r in all_credit_rows
                         if r['credit'] >= credit_threshold])
    credit_total = round(sum(r['credit'] for r in credit_rows), 2)
    credit_hidden_count = len(all_credit_rows) - len(credit_rows)

    return render_template(
        'cashflow.html',
        cash_rows=cash_rows,
        aging=aging,
        revenue_rows=revenue_rows,
        total_cash_in=total_cash_in,
        total_receipts=total_receipts,
        total_outstanding=total_outstanding,
        total_open_count=total_open_count,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
        credit_rows=credit_rows,
        credit_total=credit_total,
        credit_hidden_count=credit_hidden_count,
        show_all_credit=show_all_credit,
    )


# ── Revenue Dashboard ─────────────────────────────────────────────────────────

@app.route('/revenue')
def revenue_dashboard():
    """Revenue dashboard: monthly revenue (accrual) + accrual-vs-cash
    side-by-side + top customers + top brands + period KPIs.

    Admin + manager only (same gating as cashflow_dashboard).
    Optional ?from=YYYY-MM&to=YYYY-MM period filter.
    Default: last 12 months ending today's month.
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None

    def _month_start(ym):
        return ym + '-01'

    def _month_end(ym):
        import calendar as _cal
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    # Default: last 12 calendar months ending today's month (inclusive).
    # Subtract 11 from (year*12 + month-1) to land on the same month one year ago + 1.
    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    date_from = _month_start(from_month)
    date_to   = _month_end(to_month)

    summary       = rev_mod.revenue_summary(date_from=date_from, date_to=date_to)
    revenue_rows  = cf_mod.revenue_by_month(date_from=date_from, date_to=date_to)
    cash_rows     = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    top_customers = rev_mod.top_customers_by_revenue(
                        date_from=date_from, date_to=date_to, limit=20)
    top_brands    = rev_mod.top_brands_by_revenue(
                        date_from=date_from, date_to=date_to, limit=10)

    # Accrual-vs-Cash by month: full outer join in Python so gaps show as 0.
    months = sorted({r['month'] for r in revenue_rows} |
                    {r['month'] for r in cash_rows})
    rev_by_m  = {r['month']: r['revenue'] for r in revenue_rows}
    cash_by_m = {r['month']: r['cash_in']  for r in cash_rows}
    month_compare = []
    for m in months:
        rev_v  = rev_by_m.get(m, 0.0)
        cash_v = cash_by_m.get(m, 0.0)
        month_compare.append({
            'month':   m,
            'revenue': round(rev_v, 2),
            'cash_in': round(cash_v, 2),
            'gap':     round(rev_v - cash_v, 2),
        })

    total_cash_in = round(sum(r['cash_in'] for r in cash_rows), 2)

    return render_template(
        'revenue.html',
        total_revenue=summary['total_revenue'],
        total_invoices=summary['total_invoices'],
        total_customers=summary['total_customers'],
        aov=summary['aov'],
        total_cash_in=total_cash_in,
        revenue_rows=revenue_rows,
        cash_rows=cash_rows,
        month_compare=month_compare,
        top_customers=top_customers,
        top_brands=top_brands,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
    )


@app.route('/revenue/unmapped')
def revenue_unmapped_drilldown():
    """Drill into the 'ไม่ระบุแบรนด์' bucket from /revenue.

    Shows ranked list of (unmapped BSN code) + (no-brand product) items
    so mapping work can target the biggest items first. Admin + manager
    only. Optional ?from=YYYY-MM&to=YYYY-MM filter (defaults to last 12
    months — mirrors /revenue).
    """
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))

    from_month = request.args.get('from') or None
    to_month   = request.args.get('to')   or None
    limit_raw  = request.args.get('limit', '100')
    try:
        limit = max(1, min(int(limit_raw), 500))
    except ValueError:
        limit = 100

    if not from_month or not to_month:
        today = date.today()
        to_month = today.strftime('%Y-%m')
        total = today.year * 12 + (today.month - 1) - 11
        fm_year, fm_month = divmod(total, 12)
        from_month = f'{fm_year:04d}-{fm_month + 1:02d}'

    import calendar as _cal
    def _month_end(ym):
        try:
            y, m = int(ym[:4]), int(ym[5:7])
            return f'{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}'
        except (ValueError, IndexError):
            return ym + '-31'

    date_from = from_month + '-01'
    date_to   = _month_end(to_month)

    rows = rev_mod.unmapped_revenue_drilldown(
        date_from=date_from, date_to=date_to, limit=limit,
    )
    bucket_total = round(sum(r['revenue'] for r in rows), 2)

    return render_template(
        'revenue_unmapped.html',
        rows=rows,
        bucket_total=bucket_total,
        from_month=from_month,
        to_month=to_month,
        limit=limit,
    )


# ── AR Follow-up workspace ───────────────────────────────────────────────────

def _arf_require_manager():
    if session.get('role') not in ('admin', 'manager', 'shareholder'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))
    return None


def _arf_require_admin():
    if session.get('role') != 'admin':
        flash('ต้องใช้บัญชี Admin', 'danger')
        return redirect(url_for('ar_dashboard', tab='customers'))
    return None


@app.route('/accounting/ar-followup')
def ar_followup():
    """Redirect stub — content moved to /ar?tab=customers (AR consolidation)."""
    return redirect(url_for('ar_dashboard', tab='customers'))


@app.route('/accounting/ar-followup/customer/<path:customer_key>')
def ar_followup_customer(customer_key):
    """Per-customer detail page. `customer_key` is the URL slug — either a
    `customer_code` (preferred, stable) or a customer name (legacy bookmark
    or orphan customer). Resolved by `arf_mod._resolve_target` inside the
    detail/followup helpers."""
    redirect_ = _arf_require_manager()
    if redirect_:
        return redirect_

    invoices = arf_mod.get_customer_ar_detail(customer=customer_key)
    followups = arf_mod.get_customer_followups(customer=customer_key)
    total_outstanding = round(sum(i['outstanding'] for i in invoices), 2)

    # Display name = name on the most recent invoice; else newest log; else key.
    if invoices:
        latest_inv = max(invoices, key=lambda i: i.get('invoice_date') or '')
        customer_name = latest_inv['customer']
        customer_code = latest_inv.get('customer_code')
    elif followups:
        customer_name = followups[0]['customer']
        customer_code = followups[0].get('customer_code')
    else:
        customer_name = customer_key
        customer_code = None

    return render_template(
        'ar_followup_detail.html',
        customer_key=customer_key,
        customer_name=customer_name,
        customer_code=customer_code,
        invoices=invoices,
        followups=followups,
        total_outstanding=total_outstanding,
        today=date.today().isoformat(),
    )


@app.route('/accounting/ar-followup/log/new', methods=['POST'])
def ar_followup_log_new():
    redirect_ = _arf_require_admin()
    if redirect_:
        return redirect_

    customer = request.form.get('customer', '').strip()
    customer_code = (request.form.get('customer_code') or '').strip() or None
    # Redirect target = URL slug of the detail page. Prefer customer_code
    # (stable) over name; fall back to customer_key form field for legacy
    # bookmarks; finally fall back to the name.
    customer_key = (request.form.get('customer_key') or '').strip() \
                   or customer_code or customer
    if not customer:
        flash('ระบุชื่อลูกค้าไม่ถูกต้อง', 'danger')
        return redirect(url_for('ar_dashboard', tab='customers'))

    def _f(name):
        v = request.form.get(name, '').strip()
        return v or None

    promised_amount = _f('promised_amount')
    try:
        promised_amount = float(promised_amount.replace(',', '')) if promised_amount else None
    except ValueError:
        promised_amount = None

    try:
        arf_mod.log_outreach(
            customer=customer,
            customer_code=customer_code,
            log_date=_f('log_date') or date.today().isoformat(),
            channel=request.form.get('channel', 'phone'),
            contact_person=_f('contact_person'),
            result=request.form.get('result', 'other'),
            promised_amount=promised_amount,
            promised_date=_f('promised_date'),
            next_action_date=_f('next_action_date'),
            notes=_f('notes'),
            created_by=session.get('display_name') or session.get('role') or 'admin',
        )
        flash('บันทึกการติดตามแล้ว', 'success')
    except sqlite3.IntegrityError as e:
        flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')

    return redirect(url_for('ar_followup_customer', customer_key=customer_key))


@app.route('/accounting/ar-followup/log/<int:log_id>/delete', methods=['POST'])
def ar_followup_log_delete(log_id):
    redirect_ = _arf_require_admin()
    if redirect_:
        return redirect_

    customer_key = (request.form.get('customer_key') or '').strip()
    arf_mod.delete_outreach(log_id=log_id)
    flash('ลบรายการแล้ว', 'success')
    if customer_key:
        return redirect(url_for('ar_followup_customer', customer_key=customer_key))
    return redirect(url_for('ar_dashboard', tab='customers'))


@app.route('/accounting/ar-followup/export.csv')
def ar_followup_export():
    redirect_ = _arf_require_manager()
    if redirect_:
        return redirect_

    rows = arf_mod.customer_ranking()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write('﻿')  # BOM so Excel reads UTF-8 Thai correctly
    w = _csv.writer(buf)
    w.writerow(['ลูกค้า', 'รหัส', '#ใบ', 'ยอดค้างรวม',
                'อายุสูงสุด(วัน)', '0-30', '31-60', '61-90', '90+',
                'ติดตามล่าสุด', 'ผลล่าสุด', 'นัดหมายถัดไป'])
    for r in rows:
        b = r['age_buckets']
        w.writerow([r['customer'], r.get('customer_code') or '',
                    r['invoice_count'], f'{r["outstanding"]:.2f}',
                    r['oldest_age_days'],
                    f'{b["0-30"]:.2f}', f'{b["31-60"]:.2f}',
                    f'{b["61-90"]:.2f}', f'{b["90+"]:.2f}',
                    r.get('last_log_date') or '',
                    r.get('last_log_result') or '',
                    r.get('next_action_date') or ''])

    from flask import Response
    fname = f'ar_followup_{date.today().strftime("%Y%m%d")}.csv'
    return Response(buf.getvalue().encode('utf-8'), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)
