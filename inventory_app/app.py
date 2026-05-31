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
import sys
import sqlite3
import shutil
import tempfile
from datetime import date, datetime

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, send_file)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import models
from database import init_db, get_connection
from parse_weekly import parse_sales, parse_purchases, detect_file_type, is_history_export
from parse_platform import (parse_shopee, parse_lazada, export_shopee, export_lazada,
                            export_mapping, parse_mapping,
                            parse_shopee_orders, parse_lazada_orders,
                            export_listing_mapping, parse_listing_mapping)
from blueprints.products import bp_products
from blueprints.supplier_catalogue import bp_supplier_catalogue
from blueprints.mobile import bp_mobile
from blueprints.hr import bp_hr
from blueprints.cashbook import bp_cashbook
import cashflow as cf_mod
import revenue as rev_mod
import ar_followup as arf_mod
import payments_alloc as pa_mod

app = Flask(__name__)
# Honor X-Forwarded-Proto/Host from Railway's edge so url_for and post-login
# redirects use https instead of http. Trust exactly one proxy hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['JSON_AS_ASCII'] = False
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['ITEMS_PER_PAGE'] = config.ITEMS_PER_PAGE
app.config['DB_ROUTES_ENABLED'] = False
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


# ── Auth ──────────────────────────────────────────────────────────────────────
#
# Roles: admin > manager > staff
#   admin   – full access + user management
#   manager – see cost/GP/payments; cannot edit products/users
#   staff   – import weekly flow + read-only views (no cost/GP)
#
# POST whitelist by role
_STAFF_POST_OK = frozenset([
    'login', 'logout',
    'import_weekly', 'import_weekly_confirm', 'mapping_save', 'unit_conversions_save', 'unit_conversions_edit',
    'products.product_location_save',
    'admin_exit_simulate',
    'conversion_new', 'conversion_edit', 'conversion_run', 'conversion_delete',
    'api_product_barcodes',
])
_MANAGER_POST_OK = _STAFF_POST_OK | frozenset([
    'import_payments', 'products.product_online_stock',
    'customer_reassign', 'customer_bulk_reassign',
    'products.product_sku_code_save', 'products.product_regen_sku_code',
    'products.product_packaging_save',
    'mapping_suggestion_approve',
    'import_credit_notes_preview', 'import_credit_notes_commit',
    'photos_review_assign', 'photos_review_delete',
])
# regions_admin POST is intentionally admin-only — gated inline at the top of
# the route. Other admin-only writes use _require_admin().
# admin can POST anything


# ── Module definitions for sidebar switcher ──────────────────────────────────
# Each entry: key, name_th, icon (bootstrap-icons class), first_endpoint
# (first_endpoint is used to build the switcher navigation target).
# Roles 'admin' and 'manager' can see 'hr'; only 'admin' sees 'admin_module'.
_MODULE_DEFS = [
    {
        'key': 'overview',
        'name': 'ภาพรวม',
        'icon': 'bi-speedometer2',
        'first_endpoint': 'dashboard',
        'roles': None,  # all roles
    },
    {
        'key': 'operation',
        'name': 'คลังสินค้า',
        'icon': 'bi-box-seam',
        'first_endpoint': 'products.product_list',
        'roles': None,
    },
    {
        'key': 'accounting',
        'name': 'การค้า & บัญชี',
        'icon': 'bi-cash-coin',
        'first_endpoint': 'trade_dashboard',  # staff-safe landing (sales/purchases/customers)
        'roles': None,  # module visible to all; only the /accounting cost link+route is admin/manager
    },
    {
        'key': 'hr',
        'name': 'บุคลากร (HR)',
        'icon': 'bi-people',
        'first_endpoint': 'hr.dashboard',
        'roles': ('admin', 'manager'),
    },
    {
        'key': 'cashbook',
        'name': 'บัญชีรับ-จ่าย',
        'icon': 'bi-journal-text',
        'first_endpoint': 'cashbook.dashboard',
        'roles': ('admin', 'manager'),
    },
    {
        'key': 'data',
        'name': 'นำเข้าข้อมูล',
        'icon': 'bi-upload',
        'first_endpoint': 'import_weekly',
        'roles': None,
    },
    {
        'key': 'admin_module',
        'name': 'ระบบ',
        'icon': 'bi-gear',
        'first_endpoint': 'user_list',
        'roles': ('admin',),
    },
]

# Map each endpoint to the module key it belongs to.
# Endpoints not listed here fall back to 'overview'.
_ENDPOINT_MODULE = {
    # overview
    'dashboard': 'overview',
    'alerts_view': 'overview',
    # operation
    'products.product_list': 'operation',
    'products.product_detail': 'operation',
    'products.product_new': 'operation',
    'products.product_edit': 'operation',
    'products.stock_in': 'operation',
    'products.stock_out': 'operation',
    'products.stock_adjust': 'operation',
    'transaction_history': 'operation',
    'conversion_list': 'operation',
    'conversion_new': 'operation',
    'conversion_edit': 'operation',
    'conversion_run': 'operation',
    'conversion_delete': 'operation',
    'conversion_deactivate': 'operation',
    'conversion_activate': 'operation',
    'conversion_history': 'operation',
    'labels_view': 'operation',
    # accounting
    'accounting_summary': 'accounting',
    'cashflow_dashboard': 'accounting',
    'revenue_dashboard': 'accounting',
    'revenue_unmapped_drilldown': 'accounting',
    'ar_followup': 'accounting',
    'ar_followup_customer': 'accounting',
    'ar_followup_log_new': 'accounting',
    'ar_followup_log_edit': 'accounting',
    'ar_followup_log_delete': 'accounting',
    'ar_followup_export': 'accounting',
    'trade_dashboard': 'accounting',
    'sales_view': 'accounting',
    'sales_doc': 'accounting',
    'purchases_view': 'accounting',
    'purchases_doc': 'accounting',
    'customer_list': 'accounting',
    'customer_summary': 'accounting',
    'customer_map': 'accounting',
    'supplier_list': 'accounting',
    'supplier_summary': 'accounting',
    'payment_status': 'accounting',
    'payment_customers': 'accounting',
    'payment_customer_detail': 'accounting',
    'import_payments': 'accounting',
    'import_credit_notes_preview': 'accounting',
    'import_credit_notes_commit': 'accounting',
    'commission_dashboard': 'accounting',
    'commission_payouts': 'accounting',
    'commission_sp': 'accounting',
    'commission_sp_invoice': 'accounting',
    'commission_export': 'accounting',
    'commission_overrides': 'accounting',
    'commission_override_new': 'accounting',
    'commission_override_edit': 'accounting',
    'commission_override_toggle': 'accounting',
    'commission_override_delete': 'accounting',
    'express_ar_dashboard': 'accounting',
    'express_ar_customer': 'accounting',
    'express_ap_dashboard': 'accounting',
    'express_import': 'accounting',
    'ecommerce': 'accounting',
    'ecommerce_import': 'accounting',
    'ecommerce_sku_edit': 'accounting',
    'ecommerce_export': 'accounting',
    'ecommerce_mapping_export': 'accounting',
    'ecommerce_mapping_import': 'accounting',
    'ecommerce_listings_import': 'accounting',
    'ecommerce_listings_mapping_export': 'accounting',
    'ecommerce_listings_mapping_import': 'accounting',
    # hr
    'hr.dashboard': 'hr',
    'hr.employee_list': 'hr',
    'hr.employee_new': 'hr',
    'hr.employee_detail': 'hr',
    'hr.employee_entitlements': 'hr',
    'hr.leave_list': 'hr',
    'hr.leave_new': 'hr',
    'hr.payroll_list': 'hr',
    'hr.payroll_detail': 'hr',
    'hr.payslip': 'hr',
    # data
    'import_weekly': 'data',
    'import_weekly_confirm': 'data',
    'mapping': 'data',
    'mapping_save': 'data',
    'mapping_suggest': 'data',
    'mapping_suggestion_approve': 'data',
    'unit_conversions': 'data',
    'unit_conversions_save': 'data',
    'unit_conversions_edit': 'data',
    'review_transactions': 'data',
    'supplier_catalogue.supplier_catalogue_list': 'data',
    'supplier_catalogue.supplier_catalogue_detail': 'data',
    'supplier_catalogue.supplier_catalogue_new': 'data',
    'supplier_catalogue.supplier_catalogue_edit': 'data',
    'supplier_catalogue.supplier_catalogue_compare': 'data',
    'supplier_catalogue.supplier_catalogue_map': 'data',
    'supplier_catalogue.supplier_quick_update': 'data',
    # cashbook
    'cashbook.dashboard':     'cashbook',
    'cashbook.account_ledger': 'cashbook',
    'cashbook.import_view':   'cashbook',
    'cashbook.export_view':   'cashbook',
    # admin_module
    'user_list': 'admin_module',
    'user_new': 'admin_module',
    'user_edit': 'admin_module',
    'toggle_db_routes': 'admin_module',
    'upload_db': 'admin_module',
    'upload_db_confirm': 'admin_module',
    'download_db': 'admin_module',
    'admin_simulate_role': 'admin_module',
    'admin_exit_simulate': 'admin_module',
    'audit_log': 'admin_module',
}


@app.context_processor
def inject_auth():
    role = session.get('role', '')
    real_role = session.get('_real_role')
    endpoint = request.endpoint or ''
    active_module = _ENDPOINT_MODULE.get(endpoint, 'overview')
    # Build the list of modules visible to the current role
    visible_modules = []
    for m in _MODULE_DEFS:
        if m['roles'] is None or role in m['roles']:
            visible_modules.append(m)
    return {
        'is_admin':      role == 'admin',
        'is_manager':    role in ('admin', 'manager'),
        'current_user':  session.get('display_name', ''),
        'current_role':  role,
        'simulating_as': role if real_role else None,
        'real_role':     real_role,
        'alert_count':   models.count_stock_alerts(),
        'db_routes_enabled': app.config['DB_ROUTES_ENABLED'],
        'pending_suggestions_count': models.count_pending_suggestions(),
        'active_module': active_module,
        'visible_modules': visible_modules,
    }


@app.before_request
def require_login():
    endpoint = request.endpoint
    # Allow static files, login page, healthcheck, and the bootstrap DB
    # upload (which is itself token-gated) without authentication.
    if endpoint in ('login', 'static', 'healthz', 'bootstrap_upload_db'):
        return
    role = session.get('role', '')
    if not role:
        flash('กรุณาเข้าสู่ระบบก่อน', 'warning')
        return redirect(url_for('login', next=request.url))
    # HR module: staff cannot access any hr.* endpoint (GET or POST)
    if (endpoint or '').startswith('hr.') and role == 'staff':
        flash('ไม่มีสิทธิ์เข้าถึงระบบบุคลากร', 'danger')
        return redirect(url_for('dashboard'))
    # Cashbook module: staff cannot access any cashbook.* endpoint (GET or POST)
    if (endpoint or '').startswith('cashbook.') and role == 'staff':
        flash('ไม่มีสิทธิ์เข้าถึงระบบบัญชีรับ-จ่าย', 'danger')
        return redirect(url_for('dashboard'))
    if request.method != 'POST':
        return
    if role == 'staff' and endpoint not in _STAFF_POST_OK:
        flash('ไม่มีสิทธิ์ดำเนินการนี้', 'danger')
        return redirect(url_for('dashboard'))
    if role == 'manager' and endpoint not in _MANAGER_POST_OK:
        flash('ต้องใช้บัญชี Admin เท่านั้น', 'danger')
        return redirect(url_for('dashboard'))


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
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง', 'danger')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('ออกจากระบบแล้ว', 'success')
    return redirect(url_for('dashboard'))


# ── User management (admin only) ──────────────────────────────────────────────

@app.route('/users')
def user_list():
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    users = conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    conn.close()
    return render_template('users.html', users=users)


@app.route('/users/new', methods=['POST'])
def user_new():
    if session.get('role') != 'admin':
        abort(403)
    username     = request.form.get('username', '').strip()
    display_name = request.form.get('display_name', '').strip()
    role         = request.form.get('role', 'staff')
    password     = request.form.get('password', '')
    if not username or not password:
        flash('กรุณากรอกชื่อผู้ใช้และรหัสผ่าน', 'danger')
        return redirect(url_for('user_list'))
    if role not in ('admin', 'manager', 'staff'):
        role = 'staff'
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users(username, password_hash, display_name, role) VALUES (?,?,?,?)",
            (username, generate_password_hash(password, method='pbkdf2:sha256'), display_name or username, role)
        )
        conn.commit()
        flash(f'เพิ่มผู้ใช้ {username} ({role}) สำเร็จ', 'success')
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
    if role not in ('admin', 'manager', 'staff'):
        role = 'staff'
    conn = get_connection()
    if new_password:
        conn.execute(
            "UPDATE users SET display_name=?, role=?, is_active=?, password_hash=? WHERE id=?",
            (display_name, role, is_active, generate_password_hash(new_password, method='pbkdf2:sha256'), uid)
        )
    else:
        conn.execute(
            "UPDATE users SET display_name=?, role=?, is_active=? WHERE id=?",
            (display_name, role, is_active, uid)
        )
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
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        flash(f'ลบผู้ใช้ {target["username"]} สำเร็จ', 'success')
    conn.close()
    return redirect(url_for('user_list'))


@app.route('/admin/simulate-role', methods=['POST'])
def admin_simulate_role():
    if session.get('role') != 'admin' and not session.get('_real_role'):
        abort(403)
    target_role = request.form.get('role', '')
    if target_role not in ('manager', 'staff'):
        flash('Role ไม่ถูกต้อง', 'danger')
        return redirect(url_for('user_list'))
    session['_real_role'] = session.get('_real_role') or 'admin'
    session['role'] = target_role
    flash(f'กำลังจำลองเป็น {target_role} — คลิก "ออกจากโหมดจำลอง" เพื่อกลับ', 'info')
    return redirect(url_for('dashboard'))


@app.route('/admin/exit-simulate', methods=['POST'])
def admin_exit_simulate():
    real_role = session.pop('_real_role', None)
    if real_role:
        session['role'] = real_role
        flash('ออกจากโหมดจำลองแล้ว กลับเป็น Admin', 'success')
    return redirect(url_for('dashboard'))


# ── Temp: Download DB (ลบออกหลังใช้) ─────────────────────────────────────────

@app.route('/admin/toggle-db-routes', methods=['POST'])
def toggle_db_routes():
    if session.get('role') != 'admin':
        abort(403)
    app.config['DB_ROUTES_ENABLED'] = not app.config['DB_ROUTES_ENABLED']
    state = 'เปิด' if app.config['DB_ROUTES_ENABLED'] else 'ปิด'
    flash(f'{state}การเข้าถึง Upload/Download Database แล้ว', 'success')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/admin/download-db')
def download_db():
    if session.get('role') != 'admin':
        abort(403)
    if not app.config['DB_ROUTES_ENABLED']:
        abort(403)
    return send_file(config.DATABASE_PATH, as_attachment=True, download_name='inventory.db')


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
    'platform_skus', 'ecommerce_listings', 'listing_bundles',
    'po_sequences', 'salespersons',
    'commission_tiers', 'commission_assignments', 'commission_overrides',
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
    if not app.config['DB_ROUTES_ENABLED']:
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
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            hold_path = os.path.join(hold_dir, f'pending-{ts}.db')
            shutil.move(tmp, hold_path)
            session['pending_upload_path'] = hold_path
            return render_template(
                'admin_upload_db.html',
                diff_rows=diff_rows,
                warnings=warnings,
                pending=True,
            )

        # Always backup the current DB before replacing.
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', 'data', 'backups')
        )
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f'inventory-pre-upload-{ts}.db')
        try:
            shutil.copy(config.DATABASE_PATH, backup_path)
        except FileNotFoundError:
            backup_path = None  # no current DB; skip backup

        shutil.move(tmp, config.DATABASE_PATH)
        if backup_path:
            flash(f'อัปโหลด DB สำเร็จ. Backup เก็บไว้ที่ {os.path.basename(backup_path)}', 'success')
        else:
            flash('อัปโหลด DB สำเร็จ', 'success')
        return redirect(url_for('dashboard'))
    return render_template('admin_upload_db.html')


@app.route('/admin/upload-db/confirm', methods=['POST'])
def upload_db_confirm():
    """Second step after warning page: actually apply the held upload."""
    if session.get('role') != 'admin':
        abort(403)
    if not app.config['DB_ROUTES_ENABLED']:
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

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'data', 'backups')
    )
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f'inventory-pre-upload-{ts}.db')
    shutil.copy(config.DATABASE_PATH, backup_path)
    shutil.move(hold_path, config.DATABASE_PATH)
    flash(f'อัปโหลด DB สำเร็จ. Backup เก็บไว้ที่ {os.path.basename(backup_path)}', 'success')
    return redirect(url_for('dashboard'))

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    low_stock_count = models.count_low_stock()
    recent_txns = models.get_recent_transactions(10)
    total_products = models.count_active_products()
    in_stock_count = models.count_in_stock()
    return render_template('dashboard.html',
                           low_stock_count=low_stock_count,
                           recent_txns=recent_txns,
                           total_products=total_products,
                           in_stock_count=in_stock_count)


# ── Alerts ────────────────────────────────────────────────────────────────────

@app.route('/alerts')
def alerts_view():
    alerts = models.get_stock_alerts()
    return render_template('alerts.html', alerts=alerts)


# ── Products — moved to blueprints/products.py ────────────────────────────────
# Routes: /products, /products/new, /products/<id>, /products/<id>/cost-history,
#         /products/<id>/pricing, /products/<id>/edit, /products/<id>/location,
#         /products/<id>/online-stock, /products/<id>/deactivate,
#         /products/<id>/trade, /products/<id>/promotions/new,
#         /promotions/<id>/deactivate, /import, /import/confirm
# (registered via bp_products above)



# ── Stock In / Out ────────────────────────────────────────────────────────────

@app.route('/products/<int:product_id>/stock-in', methods=['GET', 'POST'])
def stock_in(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    if request.method == 'POST':
        f = request.form
        try:
            qty = int(f['quantity'])
            if qty <= 0:
                raise ValueError('จำนวนต้องมากกว่า 0')
        except ValueError as e:
            flash(str(e), 'danger')
            return render_template('transactions/stock_form.html',
                                   product=product, txn_type='IN')

        unit_mode = f.get('unit_mode', 'unit')
        base_qty = models.to_base_units(qty, unit_mode, product)
        models.add_transaction(product_id, 'IN', base_qty, unit_mode,
                               reference_no=f.get('reference_no'),
                               note=f.get('note'))
        flash(f'รับสินค้าเข้า {base_qty} {product["unit_type"]} เรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=product_id))

    return render_template('transactions/stock_form.html', product=product, txn_type='IN')


@app.route('/products/<int:product_id>/stock-out', methods=['GET', 'POST'])
def stock_out(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    if request.method == 'POST':
        f = request.form
        try:
            qty = int(f['quantity'])
            if qty <= 0:
                raise ValueError('จำนวนต้องมากกว่า 0')
        except ValueError as e:
            flash(str(e), 'danger')
            return render_template('transactions/stock_form.html',
                                   product=product, txn_type='OUT')

        unit_mode = f.get('unit_mode', 'unit')
        base_qty = models.to_base_units(qty, unit_mode, product)
        current = models.get_current_stock(product_id)
        if base_qty > current:
            flash(f'สต็อกไม่พอ (มี {current} {product["unit_type"]})', 'danger')
            return render_template('transactions/stock_form.html',
                                   product=product, txn_type='OUT')

        models.add_transaction(product_id, 'OUT', -base_qty, unit_mode,
                               reference_no=f.get('reference_no'),
                               note=f.get('note'))
        flash(f'จ่ายสินค้าออก {base_qty} {product["unit_type"]} เรียบร้อย', 'success')
        return redirect(url_for('products.product_detail', product_id=product_id))

    return render_template('transactions/stock_form.html', product=product, txn_type='OUT')


@app.route('/products/<int:product_id>/adjust', methods=['GET', 'POST'])
def stock_adjust(product_id):
    product = models.get_product(product_id)
    if not product:
        flash('ไม่พบสินค้า', 'danger')
        return redirect(url_for('products.product_list'))

    # Pages that send the user here can pass ?next=/some/path (or hidden form
    # field) to return them to where they came from (e.g. /alerts inline
    # adjust). url_for guards prevent open-redirect; we only honour an
    # internal path (starts with '/').
    def _safe_next(default_endpoint, **kw):
        nxt = request.values.get('next') or ''
        if nxt.startswith('/') and not nxt.startswith('//'):
            return nxt
        return url_for(default_endpoint, **kw)

    if request.method == 'POST':
        f = request.form
        try:
            new_qty = int(f['new_quantity'])
            if new_qty < 0:
                raise ValueError('จำนวนต้องไม่ติดลบ')
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        note = f.get('note', '').strip()
        if not note:
            flash('กรุณาระบุหมายเหตุสำหรับการปรับยอด', 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        current = models.get_current_stock(product_id)
        diff = new_qty - current
        if diff == 0:
            flash('จำนวนเท่าเดิม ไม่มีการเปลี่ยนแปลง', 'info')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        models.add_transaction(product_id, 'ADJUST', diff, 'unit', note=note)
        flash(f'ปรับยอดสต็อกเป็น {new_qty} {product["unit_type"]} เรียบร้อย', 'success')
        return redirect(_safe_next('products.product_detail', product_id=product_id))

    return render_template('transactions/adjust_form.html', product=product)


# ── Transaction History ───────────────────────────────────────────────────────

@app.route('/transactions')
def transaction_history():
    product_id = request.args.get('product_id', type=int)
    txn_type = request.args.get('type', '').strip() or None
    date_from = request.args.get('date_from', '').strip() or None
    date_to = request.args.get('date_to', '').strip() or None
    page = int(request.args.get('page', 1))

    txns, total = models.get_transactions(
        product_id=product_id, txn_type=txn_type,
        date_from=date_from, date_to=date_to,
        page=page, per_page=app.config['ITEMS_PER_PAGE']
    )
    pages = (total + app.config['ITEMS_PER_PAGE'] - 1) // app.config['ITEMS_PER_PAGE']
    return render_template('transactions/history.html',
                           txns=txns, total=total, page=page, pages=pages,
                           product_id=product_id, txn_type=txn_type,
                           date_from=date_from, date_to=date_to)


# ── Promotions and CSV Import — moved to blueprints/products.py ───────────────


# ── Weekly Import (ขาย / ซื้อ) ───────────────────────────────────────────────

ALLOWED_WEEKLY = {'cp874'}

def _detect_express_kind(path):
    """Auto-classify an uploaded Express file so one upload box handles them all:
    'sales' / 'purchase' (transaction files → diff-confirm) or
    'ar_snapshot' / 'ap_snapshot' (outstanding snapshots → snapshot-replace) or
    'unknown'. AR/AP are detected first by their distinctive header markers."""
    try:
        with open(path, encoding='cp874') as f:
            head = ''.join([next(f, '') for _ in range(8)]).replace('\xa0', ' ')
    except (OSError, UnicodeDecodeError):
        return 'unknown'
    if 'ลูกหนี้คงค้าง' in head or 'รายงานลูกหนี้' in head:
        return 'ar_snapshot'
    if 'เจ้าหนี้คงค้าง' in head or 'รายงานเจ้าหนี้' in head:
        return 'ap_snapshot'
    return detect_file_type(path)


def _express_snapshot_summary(path, kind, company='BSN'):
    """Read-only summary of an AR/AP outstanding snapshot for the preview page.
    Snapshots REPLACE the prior (entity, as-of date) — there is no per-doc diff
    (a newer snapshot is the new truth), so we show as-of date, doc count, total,
    and the delta vs the snapshot currently in the DB."""
    if kind == 'ar_snapshot':
        recs = list(express_importer.p_ar.parse_ar_snapshot(path))
        as_of = express_importer.p_ar.report_asof_date(path)
        table = 'express_ar_outstanding'
    else:
        recs = list(express_importer.p_ap.parse_ap_snapshot(path))
        as_of = express_importer.p_ap.report_asof_date(path)
        table = 'express_ap_outstanding'
    total = round(sum((r.outstanding_amount or 0) for r in recs), 2)
    conn = get_connection()
    prior = conn.execute(
        f"SELECT snapshot_date_iso, ROUND(SUM(outstanding_amount),2) FROM {table} "
        f"WHERE entity=? AND snapshot_date_iso=("
        f"  SELECT MAX(snapshot_date_iso) FROM {table} WHERE entity=?) "
        f"GROUP BY snapshot_date_iso", (company, company)).fetchone()
    conn.close()
    return {
        'kind': kind, 'entity': company, 'as_of': as_of, 'n_docs': len(recs),
        'total': total,
        'prior_date': prior[0] if prior else None,
        'prior_total': prior[1] if prior else None,
        'replaces_same_date': bool(prior and prior[0] == as_of),
    }


@app.route('/import-weekly', methods=['GET', 'POST'])
def import_weekly():
    if request.method == 'POST':
        f = request.files.get('weekly_file')
        if not f or not f.filename.endswith('.csv'):
            flash('กรุณาเลือกไฟล์ .csv', 'danger')
            return redirect(url_for('import_weekly'))

        # Drop any previously-staged-but-unconfirmed file so re-previewing
        # (preview A, then preview B without confirm/cancel) never orphans a
        # temp file in pending_imports/.
        _prev = session.pop('pending_import', None)
        if _prev and _prev.get('path'):
            try:
                os.remove(_prev['path'])
            except OSError:
                pass

        # Save to a pending location so the confirm step can re-parse the exact
        # same file without a re-upload (mirrors /admin/upload-db's flow).
        pending_dir = os.path.join(config.UPLOAD_FOLDER, 'pending_imports')
        os.makedirs(pending_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = os.path.basename(f.filename)
        tmp_path = os.path.join(pending_dir, f'{ts}__{safe_name}')
        f.save(tmp_path)

        def _discard():
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        # One upload box, auto-detected: ขาย/ซื้อ transaction files go through
        # the per-doc diff-confirm; AR/AP outstanding snapshots go through the
        # snapshot-replace preview (different data shapes → different handling).
        kind = _detect_express_kind(tmp_path)

        if kind in ('sales', 'purchase'):
            entries = parse_sales(tmp_path) if kind == 'sales' else parse_purchases(tmp_path)
            if not entries:
                _discard()
                flash('ไม่พบข้อมูลในไฟล์', 'warning')
                return redirect(url_for('import_weekly'))
            # DRY-RUN: show exactly what will change (full OR partial file). A
            # full re-upload of unchanged data shows ~0 changes (safe no-op); a
            # wrong/corrupt file shows many changes → cancel. The preview
            # replaces the old hard history-block (is_history_export = info flag).
            plan = models.preview_import(entries, kind)
            session['pending_import'] = {'path': tmp_path, 'kind': kind, 'filename': safe_name}
            return render_template(
                'import_preview.html', plan=plan, file_type=kind,
                filename=safe_name, n_rows=len(entries),
                is_full=is_history_export(tmp_path),
            )

        if kind in ('ar_snapshot', 'ap_snapshot'):
            try:
                summary = _express_snapshot_summary(tmp_path, kind)
            except Exception as e:
                _discard()
                flash(f'อ่านไฟล์ AR/AP ไม่สำเร็จ: {e}', 'danger')
                return redirect(url_for('import_weekly'))
            session['pending_import'] = {
                'path': tmp_path, 'kind': kind, 'filename': safe_name, 'company': 'BSN',
            }
            return render_template('import_snapshot_preview.html',
                                   summary=summary, filename=safe_name)

        _discard()
        flash('ไม่รู้จักชนิดไฟล์ (รองรับ: ขาย / ซื้อ / ลูกหนี้คงค้าง / เจ้าหนี้คงค้าง)', 'danger')
        return redirect(url_for('import_weekly'))

    recent_imports = models.get_recent_imports(limit=5)
    return render_template('import_weekly.html', recent_imports=recent_imports)


@app.route('/import-weekly/confirm', methods=['POST'])
def import_weekly_confirm():
    """Step 2: apply the import the user previewed (or cancel)."""
    pend = session.pop('pending_import', None)
    if not pend or not os.path.exists(pend['path']):
        flash('ไม่พบไฟล์ที่รอยืนยัน — กรุณาอัปโหลดใหม่', 'danger')
        return redirect(url_for('import_weekly'))

    if request.form.get('action') == 'cancel':
        try:
            os.remove(pend['path'])
        except OSError:
            pass
        flash('ยกเลิกการนำเข้าแล้ว', 'info')
        return redirect(url_for('import_weekly'))

    kind = pend.get('kind')

    # AR/AP snapshot → idempotent snapshot-replace via the Express importer.
    if kind in ('ar_snapshot', 'ap_snapshot'):
        try:
            express_importer.run_import(kind, pend['path'],
                                        company_code=pend.get('company', 'BSN'),
                                        dry_run=False, incremental=False)
            flash(f'นำเข้า {"ลูกหนี้คงค้าง" if kind == "ar_snapshot" else "เจ้าหนี้คงค้าง"} '
                  f'({pend.get("company", "BSN")}) สำเร็จ', 'success')
        except Exception as e:
            flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        try:
            os.remove(pend['path'])
        except OSError:
            pass
        return redirect(url_for('express_ar_dashboard' if kind == 'ar_snapshot'
                                else 'express_ap_dashboard'))

    # ขาย/ซื้อ transaction file → per-doc idempotent diff import.
    entries = (parse_sales(pend['path']) if kind == 'sales'
               else parse_purchases(pend['path']))
    stats = models.import_weekly(entries, kind, pend['filename'])
    try:
        os.remove(pend['path'])
    except OSError:
        pass

    parts = []
    if stats['imported']:
        parts.append(f'นำเข้า/อัพเดท {stats["imported"]} รายการ')
    if stats['unchanged']:
        parts.append(f'เหมือนเดิม {stats["unchanged"]} รายการ')
    if stats['skipped_dup']:
        parts.append(f'ข้าม {stats["skipped_dup"]} รายการ')
    if stats['new_unmapped']:
        parts.append(f'สินค้าใหม่ไม่มีในระบบ {stats["new_unmapped"]} รายการ')
    flash('  |  '.join(parts) or 'ไม่มีการเปลี่ยนแปลง',
          'success' if stats['new_unmapped'] == 0 else 'warning')
    if models.get_pending_unit_conversions():
        return redirect(url_for('unit_conversions'))
    if stats['new_unmapped'] > 0:
        return redirect(url_for('mapping'))
    return redirect(url_for('sales_view') if kind == 'sales' else url_for('purchases_view'))


# ── Unit Conversions ──────────────────────────────────────────────────────────

@app.route('/unit-conversions')
def unit_conversions():
    search = request.args.get('q', '').strip()
    page = int(request.args.get('page', 1))
    per_page = app.config['ITEMS_PER_PAGE']
    pending = models.get_pending_unit_conversions(search=search or None)
    existing, total = models.get_all_unit_conversions(
        search=search or None, page=page, per_page=per_page
    )
    pages = (total + per_page - 1) // per_page
    return render_template('unit_conversions.html',
                           pending=pending, existing=existing,
                           search=search, page=page, pages=pages, total=total)


@app.route('/unit-conversions/save', methods=['POST'])
def unit_conversions_save():
    # Pass 1: full-unit names Put typed for unknown acronyms
    # key: "fullunit_<product_id>_<acronym>"
    learned = {}                       # acronym -> full
    acr_full = {}                      # (pid_str, acronym) -> full
    for key, val in request.form.items():
        if key.startswith('fullunit_'):
            parts = key[9:].split('_', 1)
            full = (val or '').strip()
            if len(parts) == 2 and full:
                learned[parts[1]] = full
                acr_full[(parts[0], parts[1])] = full
    if learned:
        # persist to bsn_unit_full.json + normalise the whole ledger
        models.learn_acronyms_normalize(learned)

    items = []
    for key, val in request.form.items():
        # key format: "ratio_<product_id>_<bsn_unit>"
        if key.startswith('ratio_'):
            parts = key[6:].split('_', 1)
            if len(parts) == 2:
                try:
                    ratio = float(val)
                    if ratio > 0:
                        pid_s, bsn_unit = parts[0], parts[1]
                        # if Put named this acronym, store conv under the
                        # FULL unit (ledger was just normalised to match)
                        bsn_unit = acr_full.get((pid_s, bsn_unit), bsn_unit)
                        items.append({'product_id': int(pid_s),
                                      'bsn_unit': bsn_unit, 'ratio': ratio})
                except (ValueError, IndexError):
                    pass
    if items:
        models.save_unit_conversions(items)
        msg = f'บันทึกการแปลงหน่วย {len(items)} รายการเรียบร้อย'
        if learned:
            msg += (f'  |  เรียนรู้หน่วยใหม่ {len(learned)} ตัว '
                    f'(จำไว้ใช้ครั้งต่อไป)')
        flash(msg, 'success')
    return redirect(url_for('unit_conversions'))


@app.route('/unit-conversions/edit', methods=['POST'])
def unit_conversions_edit():
    product_id = request.form.get('product_id', type=int)
    bsn_unit   = request.form.get('bsn_unit', '').strip()
    new_ratio  = request.form.get('ratio', type=float)
    if product_id and bsn_unit and new_ratio and new_ratio > 0:
        models.update_unit_conversion_ratio(product_id, bsn_unit, new_ratio)
        flash(f'อัปเดต ratio สำหรับ {bsn_unit} เรียบร้อย (re-sync แล้ว)', 'success')
    return redirect(url_for('unit_conversions'))


# ── Review uncertain no-ref transactions ──────────────────────────────────────

@app.route('/review-transactions')
def review_transactions():
    rows = models.get_uncertain_no_ref_transactions()
    return render_template('review_transactions.html', rows=rows)


@app.route('/review-transactions/delete', methods=['POST'])
def review_transactions_delete():
    ids_str = request.form.getlist('delete_ids')
    ids = []
    for v in ids_str:
        try:
            ids.append(int(v))
        except ValueError:
            pass
    if ids:
        models.delete_transactions_by_ids(ids)
        flash(f'ลบ {len(ids)} รายการเรียบร้อย', 'success')
    else:
        flash('ไม่ได้เลือกรายการที่จะลบ', 'info')
    return redirect(url_for('review_transactions'))


# ── Product Code Mapping ──────────────────────────────────────────────────────

@app.route('/mapping')
def mapping():
    pending = models.get_pending_mappings()
    pending_suggestions = models.get_pending_suggestions()
    conn = get_connection()
    all_products = conn.execute("""
        SELECT p.id, p.sku, p.product_name, p.unit_type,
               COALESCE(s.quantity, 0) AS stock
          FROM products p
          LEFT JOIN stock_levels s ON s.product_id = p.id
         WHERE p.is_active = 1
         ORDER BY p.sku
    """).fetchall()
    next_sku = conn.execute("SELECT COALESCE(MAX(sku),0)+1 FROM products").fetchone()[0]
    brands = conn.execute(
        "SELECT id, name, name_th FROM brands ORDER BY is_own_brand DESC, sort_order, name"
    ).fetchall()
    color_codes = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY sort_order, code"
    ).fetchall()
    # mig 061: per-unit override rows (bsn_unit<>'') — shown read-only so
    # Put can see which codes are split by unit.
    overrides = conn.execute("""
        SELECT m.bsn_code, m.bsn_name, m.bsn_unit, m.product_id,
               p.sku, p.product_name, p.unit_type
          FROM product_code_mapping m
          JOIN products p ON p.id = m.product_id
         WHERE m.bsn_unit <> ''
         ORDER BY m.bsn_code, m.bsn_unit
    """).fetchall()
    conn.close()
    tab = request.args.get('tab', 'mapping')
    return render_template(
        'mapping.html',
        pending=pending,
        pending_suggestions=pending_suggestions,
        all_products=all_products,
        next_sku=next_sku,
        brands=brands,
        color_codes=color_codes,
        overrides=overrides,
        active_tab=tab,
    )


@app.route('/mapping/suggest/<bsn_code>')
def mapping_suggest(bsn_code):
    """Return JSON: top fuzzy matches + parsed fields + cost/unit
    for the smart-suggest modal on /mapping."""
    if not session.get('role'):
        abort(403)
    conn = get_connection()
    # mig 061: a code may now have multiple rows (catch-all + overrides).
    # LIMIT 1 (catch-all preferred) keeps bsn_name stable for the modal.
    row = conn.execute(
        "SELECT bsn_code, bsn_name FROM product_code_mapping "
        "WHERE bsn_code = ? ORDER BY (bsn_unit = '') DESC LIMIT 1",
        (bsn_code,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'unknown bsn_code'}), 404
    import bsn_suggest
    out = bsn_suggest.suggest_for_bsn(conn, bsn_code, row['bsn_name'])
    conn.close()
    return jsonify(out)


@app.route('/mapping/save', methods=['POST'])
def mapping_save():
    data = request.get_json()
    user_id = session.get('user_id')
    for item in data.get('mappings', []):
        bsn_code = item.get('bsn_code')
        action   = item.get('action')       # 'map', 'new', 'ignore', 'stage'
        if action == 'map':
            pid = int(item['product_id'])
            # mig 061: optional per-unit override. map_bsn_unit='' (default)
            # = the catch-all row → unchanged behavior. A non-empty value
            # creates/updates a (bsn_code, unit) override → that product.
            map_unit = (item.get('map_bsn_unit') or '').strip()
            models.upsert_mapping(bsn_code, item['bsn_name'], product_id=pid,
                                  bsn_unit=map_unit)
            # Optional: capture unit_conversion at map time when BSN unit ≠ product unit
            bsn_unit = (item.get('bsn_unit') or '').strip()
            ratio = item.get('unit_conversion_ratio')
            if bsn_unit and ratio:
                try:
                    r = float(ratio)
                except (TypeError, ValueError):
                    r = 0
                if r > 0:
                    models.upsert_unit_conversion(pid, bsn_unit, r)
        elif action == 'new':
            # legacy quick-create — admin-only path. Still supported but
            # smart-suggest flow uses 'stage' instead so manager review applies.
            if session.get('role') != 'admin':
                continue
            try:
                sku_to_use = int(item.get('new_sku') or 0)
            except (ValueError, TypeError):
                sku_to_use = 0
            if not sku_to_use:
                sku_to_use = get_connection().execute(
                    "SELECT COALESCE(MAX(sku),0)+1 FROM products"
                ).fetchone()[0]
            pid = models.create_product({
                'sku': sku_to_use,
                'product_name': item.get('new_name') or item['bsn_name'],
                'units_per_carton': None,
                'units_per_box': None,
                'unit_type': 'ตัว',
                'hard_to_sell': 0,
                'cost_price': 0.0,
                'base_sell_price': 0.0,
                'low_stock_threshold': config.LOW_STOCK_DEFAULT_THRESHOLD,
                'shopee_stock': 0,
                'lazada_stock': 0,
            })
            models.upsert_mapping(bsn_code, item['bsn_name'], product_id=pid)
        elif action == 'stage':
            # Smart-suggest flow: stage new SKU for manager/admin review
            payload = {
                'bsn_code': bsn_code,
                'bsn_name': item['bsn_name'],
                'suggested_name': item.get('suggested_name'),
                'category': item.get('category'),
                'series': item.get('series'),
                'brand_id': item.get('brand_id') or None,
                'model': item.get('model'),
                'size': item.get('size'),
                'color_th': item.get('color_th'),
                'color_code': item.get('color_code') or None,
                'packaging': item.get('packaging') or None,
                'condition': item.get('condition'),
                'pack_variant': item.get('pack_variant'),
                'suggested_cost': float(item.get('suggested_cost') or 0),
                'suggested_unit_type': item.get('suggested_unit_type') or 'ตัว',
                'units_per_carton': item.get('units_per_carton'),
                'units_per_box': item.get('units_per_box'),
                # Round-2 extras (mig 037)
                'brand_other_name': item.get('brand_other_name') or None,
                'color_code_other': item.get('color_code_other') or None,
                'packaging_other': item.get('packaging_other') or None,
                'bsn_unit': item.get('bsn_unit') or None,
                'unit_conversion_ratio': (
                    float(item['unit_conversion_ratio'])
                    if item.get('unit_conversion_ratio') else None
                ),
            }
            models.save_pending_suggestion(payload, user_id)
        elif action == 'ignore':
            models.upsert_mapping(
                bsn_code, item['bsn_name'],
                is_ignored=1,
                ignore_reason=item.get('ignore_reason') or None,
            )

    # Backfill product_id on existing unlinked rows
    conn = get_connection()
    models.resolve_pending_mappings(conn)
    conn.close()

    pending_left = len(models.get_pending_mappings())
    pending_sugg = models.count_pending_suggestions()
    return jsonify({'ok': True, 'pending_left': pending_left,
                    'pending_suggestions': pending_sugg})


@app.route('/mapping/suggestions/<int:sid>/approve', methods=['POST'])
def mapping_suggestion_approve(sid):
    """Manager/admin approves a staged SKU suggestion.
    Body may include edits to override staged fields before product creation."""
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    edits = request.get_json() or {}
    # cast brand_id to int if present
    if edits.get('brand_id'):
        try:
            edits['brand_id'] = int(edits['brand_id'])
        except (TypeError, ValueError):
            edits['brand_id'] = None
    if edits.get('suggested_cost') is not None:
        try:
            edits['suggested_cost'] = float(edits['suggested_cost'])
        except (TypeError, ValueError):
            edits['suggested_cost'] = 0.0
    try:
        new_pid = models.approve_pending_suggestion(
            sid, edits, session.get('user_id')
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': True, 'product_id': new_pid})


# ── Sales View ────────────────────────────────────────────────────────────────

@app.route('/trade-dashboard')
def trade_dashboard():
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    stats = models.get_trade_dashboard(date_from, date_to)
    return render_template('trade_dashboard.html', stats=stats)


@app.route('/customers')
def customer_list():
    search    = request.args.get('q', '').strip()
    region_id = request.args.get('region_id', '').strip()
    region    = request.args.get('region', '').strip()  # legacy bookmarks
    page      = request.args.get('page', 1, type=int) or 1
    per_page  = app.config['ITEMS_PER_PAGE']

    # Legacy ?region=<text>: warn the user when it doesn't resolve so we don't
    # silently show all customers and look like the filter is broken.
    if region and not region_id:
        from database import get_connection as _gc
        _conn = _gc()
        match = _conn.execute(
            "SELECT id FROM regions WHERE code = ? OR name_th = ? LIMIT 1",
            (region, region),
        ).fetchone()
        _conn.close()
        if not match:
            flash(f'ไม่พบเขต "{region}" — แสดงลูกค้าทั้งหมดแทน', 'warning')

    customers, total = models.get_customers(
        search=search or None,
        region=region or None,
        region_id=region_id or None,
        page=page, per_page=per_page,
    )
    pages   = (total + per_page - 1) // per_page
    regions = models.get_regions()
    return render_template('customers.html',
                           customers=customers, total=total,
                           page=page, pages=pages,
                           search=search, region=region, region_id=region_id,
                           regions=regions)


@app.route('/customer/<path:customer_name>')
def customer_summary(customer_name):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    data = models.get_customer_summary(customer_name, date_from, date_to)
    unpaid_bills = models.get_customer_unpaid_bills(customer_name)
    unpaid_total = sum(b['total_net'] or 0 for b in unpaid_bills)

    master = models.get_customer_master(data['customer_code']) if data.get('customer_code') else None
    return render_template('customer_summary.html',
                           data=data,
                           unpaid_bills=unpaid_bills, unpaid_total=unpaid_total,
                           master=master,
                           salespersons=models.get_active_salespersons(),
                           regions=models.get_all_regions(),
                           orphan_codes=models.get_orphan_salesperson_codes())


@app.route('/customer/<customer_code>/reassign', methods=['POST'])
def customer_reassign(customer_code):
    salesperson = request.form.get('salesperson', '').strip()
    region_id   = request.form.get('region_id', '').strip()

    result = models.update_customer_assignment(customer_code, salesperson, region_id)
    if result['ok']:
        flash('บันทึก salesperson / region เรียบร้อย (master record)', 'success')
    else:
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    # Use the canonical name from the master record so the post-redirect
    # destination can never be steered by a hostile form value.
    master = models.get_customer_master(customer_code)
    if master:
        return redirect(url_for('customer_summary', customer_name=master['name']))
    return redirect(url_for('customer_list'))


@app.route('/customers/bulk-reassign', methods=['GET', 'POST'])
def customer_bulk_reassign():
    if session.get('role') not in ('admin', 'manager'):
        abort(403)

    if request.method == 'POST':
        codes       = request.form.getlist('customer_codes')
        salesperson = request.form.get('salesperson', '').strip()
        region_id   = request.form.get('region_id', '').strip()
        mode        = request.form.get('mode', 'salesperson')

        result = models.bulk_reassign_customers(codes, salesperson, region_id, mode=mode)
        if result['ok']:
            flash(f'อัปเดต {result["updated"]} ลูกค้าเรียบร้อย (master record)', 'success')
        else:
            flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')
        redirect_args = {
            'q':                  request.form.get('q', '') or None,
            'salesperson_filter': request.form.get('salesperson_filter', '') or None,
            'region_filter':      request.form.get('region_filter', '') or None,
            'orphan':             '1' if request.form.get('orphan_filter') == '1' else None,
        }
        return redirect(url_for('customer_bulk_reassign',
                                **{k: v for k, v in redirect_args.items() if v}))

    search        = request.args.get('q', '').strip()
    salesperson_f = request.args.get('salesperson_filter', '').strip()
    region_f      = request.args.get('region_filter', '').strip()
    orphan_only   = request.args.get('orphan') == '1'
    page          = request.args.get('page', 1, type=int) or 1
    per_page      = 100

    region_id_int = int(region_f) if region_f.isdigit() else None
    customers, total = models.get_customers_master(
        search=search or None,
        salesperson=salesperson_f or None,
        region_id=region_id_int,
        orphan_only=orphan_only,
        page=page, per_page=per_page,
    )
    pages = (total + per_page - 1) // per_page

    return render_template(
        'customers_bulk_reassign.html',
        customers=customers, total=total, page=page, pages=pages,
        search=search, salesperson_filter=salesperson_f,
        region_filter=region_f, orphan_only=orphan_only,
        salespersons=models.get_active_salespersons(),
        regions=models.get_all_regions(),
        orphan_codes=models.get_orphan_salesperson_codes(),
    )


@app.route('/suppliers')
def supplier_list():
    search   = request.args.get('q', '').strip()
    page     = int(request.args.get('page', 1))
    per_page = app.config['ITEMS_PER_PAGE']
    suppliers, total = models.get_suppliers(
        search=search or None, page=page, per_page=per_page
    )
    pages = (total + per_page - 1) // per_page
    return render_template('suppliers.html',
                           suppliers=suppliers, total=total,
                           page=page, pages=pages, search=search)


@app.route('/supplier/<path:supplier_name>')
def supplier_summary(supplier_name):
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    data = models.get_supplier_summary(supplier_name, date_from, date_to)
    return render_template('supplier_summary.html', data=data)


@app.route('/photos/<path:filepath>')
def serve_catalog_photo(filepath):
    """Serve product photos from Design/photos/ (new layout 2026-05-25).

    Old layout was Design/Catalog/photos/products/<category>/<bucket>/...; rebuilt
    by Design/Catalog/scripts/rebuild_photo_index.py into Design/photos/<family_code>/.
    """
    if not session.get('role'):
        abort(403)
    photos_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', 'Design', 'photos'
    ))
    full = os.path.normpath(os.path.join(photos_root, filepath))
    # Separator-aware containment: a bare startswith would also admit sibling
    # dirs sharing the prefix (e.g. ../photos_backup). commonpath compares
    # whole path components.
    try:
        if os.path.commonpath((full, photos_root)) != photos_root:
            abort(403)  # path traversal attempt
    except ValueError:
        abort(403)  # different drive / mixed abs-rel → reject
    if not os.path.isfile(full):
        abort(404)
    return send_file(full)


# ── Photo reviewer (clears Design/photos/_review/ queue) ─────────────────────
# One photo at a time + keyboard shortcuts. Atomic: move file + INSERT product_images.
_REVIEW_ROOT_REL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'Design', 'photos', '_review'))
_PHOTOS_ROOT_REL = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'Design', 'photos'))
_IMG_EXTS_REV = ('.png', '.jpg', '.jpeg', '.webp')
_SINGLETON_NOTE = 'auto-singleton-from-photo-import-2026-05-25'


def _walk_review_files():
    """Return sorted list of (rel_path_from__review, abs_path) for unprocessed photos."""
    out = []
    if not os.path.isdir(_REVIEW_ROOT_REL):
        return out
    for root, _dirs, files in os.walk(_REVIEW_ROOT_REL):
        for fn in files:
            if fn.lower().endswith(_IMG_EXTS_REV):
                ab = os.path.join(root, fn)
                rel = os.path.relpath(ab, _REVIEW_ROOT_REL)
                out.append((rel, ab))
    out.sort(key=lambda x: x[0])
    return out


@app.route('/photos/review')
def photos_review():
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    files = _walk_review_files()
    after = request.args.get('after') or ''
    current = None
    if after:
        for rel, ab in files:
            if rel > after:
                current = (rel, ab)
                break
    else:
        current = files[0] if files else None
    remaining = len(files)
    return render_template('photos_review.html',
                           current=current, remaining=remaining, after=after)


def _safe_under(root, candidate):
    """Reject path traversal: candidate must resolve inside root."""
    full = os.path.normpath(os.path.join(root, candidate))
    try:
        if os.path.commonpath((full, root)) != root:
            return None
    except ValueError:
        return None
    return full


@app.route('/photos/review/assign', methods=['POST'])
def photos_review_assign():
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    src_rel = (request.form.get('src') or '').strip()
    sku_id = request.form.get('sku_id', type=int)
    role = (request.form.get('role') or 'extra').strip()
    if role not in ('single', 'pack', 'family', 'extra'):
        role = 'extra'
    if not src_rel or not sku_id:
        return jsonify({'ok': False, 'error': 'missing src or sku_id'}), 400
    src_abs = _safe_under(_REVIEW_ROOT_REL, src_rel)
    if not src_abs or not os.path.isfile(src_abs):
        return jsonify({'ok': False, 'error': 'source file not found'}), 404

    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT id, sku_code, product_name, brand_id, family_id "
            "FROM products WHERE id=?", (sku_id,)
        )
        prod = cur.fetchone()
        if not prod:
            return jsonify({'ok': False, 'error': 'sku not in db'}), 404

        # If product has no family, auto-create singleton (mirror rebuild_photo_index logic)
        family_id = prod['family_id']
        if not family_id:
            fam_code = prod['sku_code']
            existing = conn.execute(
                "SELECT id FROM product_families WHERE family_code=?", (fam_code,)
            ).fetchone()
            if existing:
                family_id = existing['id']
            else:
                ins = conn.execute(
                    "INSERT INTO product_families "
                    "(family_code, display_name, brand_id, note) VALUES (?,?,?,?)",
                    (fam_code, prod['product_name'], prod['brand_id'], _SINGLETON_NOTE)
                )
                family_id = ins.lastrowid
            conn.execute(
                "UPDATE products SET family_id=? WHERE id=? AND family_id IS NULL",
                (family_id, prod['id'])
            )

        fam = conn.execute(
            "SELECT family_code FROM product_families WHERE id=?", (family_id,)
        ).fetchone()
        family_code = fam['family_code']

        # Construct target filename: sku_<code>_<role>_<orig>  (preserves original for uniqueness)
        orig_name = os.path.basename(src_rel)
        target_name = f"sku_{prod['sku_code']}_{role}_{orig_name}"
        target_dir = os.path.join(_PHOTOS_ROOT_REL, family_code)
        os.makedirs(target_dir, exist_ok=True)
        target_abs = os.path.join(target_dir, target_name)
        # Avoid collision
        if os.path.exists(target_abs):
            base, ext = os.path.splitext(target_name)
            i = 2
            while os.path.exists(os.path.join(target_dir, f"{base}_{i}{ext}")):
                i += 1
            target_name = f"{base}_{i}{ext}"
            target_abs = os.path.join(target_dir, target_name)

        # image_path stored relative to Design/photos/ (matches Flask /photos/ route)
        image_path = f"{family_code}/{target_name}"
        sort_order = {'single': 10, 'pack': 20, 'family': 50, 'extra': 30}[role]
        store_sku_id = None if role == 'family' else prod['id']
        conn.execute(
            "INSERT INTO product_images "
            "(family_id, sku_id, image_path, presentation_tag, sort_order) "
            "VALUES (?,?,?,?,?)",
            (family_id, store_sku_id, image_path, role, sort_order)
        )
        # Move file last so DB write rolls back if file move fails
        shutil.move(src_abs, target_abs)
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'next_url': url_for('photos_review')})


@app.route('/photos/review/delete', methods=['POST'])
def photos_review_delete():
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    src_rel = (request.form.get('src') or '').strip()
    if not src_rel:
        return jsonify({'ok': False, 'error': 'missing src'}), 400
    src_abs = _safe_under(_REVIEW_ROOT_REL, src_rel)
    if not src_abs or not os.path.isfile(src_abs):
        return jsonify({'ok': False, 'error': 'source file not found'}), 404
    os.remove(src_abs)
    return jsonify({'ok': True, 'next_url': url_for('photos_review')})


# ── Product walkthrough (catalog-building review) ────────────────────────────
# Paginates active products by sku_code; each page shows 2-4 products with current
# photo + assign-from-_review/ + edit link. Phase 1: no persistent skip state.

@app.route('/products/walkthrough')
def products_walkthrough():
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    page = max(1, request.args.get('page', 1, type=int))
    per_page = request.args.get('per_page', 4, type=int)
    per_page = max(2, min(per_page, 4))  # clamp 2-4

    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE is_active=1"
    ).fetchone()['c']
    rows = conn.execute(
        """
        SELECT p.id, p.sku, p.sku_code, p.product_name, p.base_sell_price,
               p.unit_type, p.family_id,
               b.short_code AS brand_short, b.name AS brand_name,
               COALESCE(s.quantity, 0) AS stock,
               f.family_code, f.display_name AS family_name,
               -- Prefer SKU-specific photo; fall back to family-level (sku_id NULL).
               -- Without this, families with multiple SKUs all render with the
               -- same (often wrong) photo on every card.
               COALESCE(
                 (SELECT image_path FROM product_images
                   WHERE sku_id = p.id
                   ORDER BY COALESCE(sort_order, 999), id LIMIT 1),
                 (SELECT image_path FROM product_images
                   WHERE family_id = p.family_id AND sku_id IS NULL
                   ORDER BY COALESCE(sort_order, 999), id LIMIT 1)
               ) AS image_path
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN product_families f ON f.id = p.family_id
          LEFT JOIN stock_levels s ON s.product_id = p.id
         WHERE p.is_active = 1
         ORDER BY p.sku_code IS NULL, p.sku_code
         LIMIT ? OFFSET ?
        """,
        (per_page, (page - 1) * per_page),
    ).fetchall()
    conn.close()

    total_pages = (total + per_page - 1) // per_page
    return render_template('products_walkthrough.html',
                           products=[dict(r) for r in rows],
                           page=page, per_page=per_page,
                           total=total, total_pages=total_pages)


@app.route('/api/photos/review-queue')
def api_photos_review_queue():
    """JSON list of _review/ photo URLs for the picker modal."""
    if session.get('role') not in ('admin', 'manager'):
        abort(403)
    files = _walk_review_files()
    limit = min(request.args.get('limit', 60, type=int), 200)
    offset = max(0, request.args.get('offset', 0, type=int))
    out = []
    for rel, _ab in files[offset:offset + limit]:
        out.append({
            'rel': rel,
            'url': url_for('serve_catalog_photo', filepath='_review/' + rel),
        })
    return jsonify({'total': len(files), 'items': out})


@app.route('/sales')
def sales_view():
    today = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()
    pid_raw   = request.args.get('product_id', '').strip()
    product_id = int(pid_raw) if pid_raw.isdigit() else None
    if product_id:
        default_from = '2020-01-01'
        default_to   = today.isoformat()
    date_from = request.args.get('date_from', '').strip() or default_from
    date_to   = request.args.get('date_to',   '').strip() or default_to
    vat_raw   = request.args.get('vat_type',  '').strip()
    vat_type  = int(vat_raw) if vat_raw.isdigit() else None
    page      = int(request.args.get('page', 1))
    per_page  = app.config['ITEMS_PER_PAGE']

    filter_product = models.get_product(product_id) if product_id else None

    rows, total = models.get_sales(
        product_id=product_id, date_from=date_from, date_to=date_to,
        vat_type=vat_type, page=page, per_page=per_page
    )
    summary = models.get_sales_summary(date_from=date_from, date_to=date_to)
    pages   = (total + per_page - 1) // per_page

    # Build summary dict keyed by vat_type (convert Row → plain dict)
    vat_summary = {r['vat_type']: dict(r) for r in summary}

    return render_template('sales.html',
                           rows=rows, total=total, pages=pages, page=page,
                           date_from=date_from, date_to=date_to,
                           vat_type=vat_type, vat_summary=vat_summary,
                           product_id=product_id, filter_product=filter_product,
                           pending_map=len(models.get_pending_mappings()))


# ── Sales Doc Detail ─────────────────────────────────────────────────────────

@app.route('/sales/doc/<doc_base>')
def sales_doc(doc_base):
    rows = models.get_sales_by_doc(doc_base)
    if not rows:
        return "ไม่พบเอกสาร", 404
    total_net = sum(r['net'] or 0 for r in rows)
    return render_template('sales_doc.html', rows=rows, doc_base=doc_base,
                           total_net=total_net,
                           pending_map=len(models.get_pending_mappings()))


# ── Purchases View ────────────────────────────────────────────────────────────

@app.route('/purchases')
def purchases_view():
    today = date.today()
    default_from = today.replace(day=1).isoformat()
    default_to   = today.isoformat()
    date_from = request.args.get('date_from', '').strip() or default_from
    date_to   = request.args.get('date_to',   '').strip() or default_to
    page      = int(request.args.get('page', 1))
    per_page  = app.config['ITEMS_PER_PAGE']

    rows, total = models.get_purchases(
        date_from=date_from, date_to=date_to,
        page=page, per_page=per_page
    )
    pages = (total + per_page - 1) // per_page

    return render_template('purchases.html',
                           rows=rows, total=total, pages=pages, page=page,
                           date_from=date_from, date_to=date_to,
                           pending_map=len(models.get_pending_mappings()))


# ── Purchases Doc Detail ─────────────────────────────────────────────────────

@app.route('/purchases/doc/<doc_base>')
def purchases_doc(doc_base):
    rows = models.get_purchases_by_doc(doc_base)
    if not rows:
        return "ไม่พบเอกสาร", 404
    total_net = sum(r['net'] or 0 for r in rows)
    return render_template('purchases_doc.html', rows=rows, doc_base=doc_base,
                           total_net=total_net,
                           pending_map=len(models.get_pending_mappings()))


# ── Payment Status ────────────────────────────────────────────────────────────

@app.route('/payment-status')
def payment_status():
    status   = request.args.get('status', 'all')   # all | paid | unpaid
    search   = request.args.get('q', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to   = request.args.get('date_to',   '').strip()
    page      = int(request.args.get('page', 1))
    per_page  = app.config['ITEMS_PER_PAGE']

    rows, total = models.get_payment_status(
        status=status, search=search,
        date_from=date_from, date_to=date_to,
        page=page, per_page=per_page
    )
    summary = models.get_payment_summary()
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        'payment_status.html',
        rows=rows, total=total,
        summary=summary,
        status=status, search=search,
        date_from=date_from, date_to=date_to,
        page=page, total_pages=total_pages,
    )


@app.route('/payment-status/customers')
def payment_customers():
    search       = request.args.get('q', '').strip()
    match_str    = request.args.get('match', '').strip()
    rows         = models.get_customer_debt_summary(search=search)
    total_outstanding = sum(r['outstanding_amount'] or 0 for r in rows)

    candidates = []
    match_amount = None
    if match_str:
        try:
            match_amount = float(match_str.replace(',', ''))
            candidates = models.find_payment_candidates(match_amount)
        except ValueError:
            pass

    return render_template(
        'payment_customers.html',
        rows=rows,
        search=search,
        total_outstanding=total_outstanding,
        match_str=match_str,
        match_amount=match_amount,
        candidates=candidates,
    )


@app.route('/payment-status/customer/<path:customer_name>')
def payment_customer_detail(customer_name):
    bills = models.get_customer_unpaid_bills(customer_name)
    total = sum(b['total_net'] or 0 for b in bills)
    return render_template(
        'payment_customer_detail.html',
        customer_name=customer_name,
        bills=bills,
        total=total,
    )


@app.route('/import-payments', methods=['POST'])
def import_payments():
    if session.get('role') not in ('admin', 'manager'):
        flash('ต้องเป็น Admin หรือ Manager', 'danger')
        return redirect(url_for('payment_status'))
    f = request.files.get('payment_file')
    if not f or not f.filename.endswith('.csv'):
        flash('กรุณาเลือกไฟล์ .csv', 'danger')
        return redirect(url_for('payment_status'))
    tmp_path = os.path.join(config.UPLOAD_FOLDER, f.filename)
    f.save(tmp_path)
    result = models.import_payments(tmp_path)
    flash(
        f'นำเข้าสำเร็จ {result["imported"]} ใบเสร็จใหม่  |  อัปเดต {result["updated"]} ใบเสร็จเดิม  |  ข้ามซ้ำ {result["skipped"]} รายการ',
        'success'
    )
    return redirect(url_for('payment_status'))


# ── Credit-note import (ใบลดหนี้): preview + commit ───────────────────────────
#
# Two-stage flow because credit_note_amounts (mig 062) is the AUTHORITATIVE
# per-SR credited amount used by payments_alloc.  Importing a stale file would
# silently overwrite live values via ON CONFLICT.  The preview stage runs the
# full importer in a transaction and rolls back, surfacing which credit_note_
# amounts rows would CHANGE so Put can decide before committing.

_CN_PREVIEW_DIR = 'cn-preview'  # subfolder under UPLOAD_FOLDER
_CN_PREVIEW_TTL_SEC = 60 * 60   # 1 hour


def _cn_preview_dir():
    p = os.path.join(config.UPLOAD_FOLDER, _CN_PREVIEW_DIR)
    os.makedirs(p, exist_ok=True)
    return p


def _cn_preview_path(token):
    """Resolve a token to its preview file path.  Token is the saved filename
    (uuid.csv).  Returns None if invalid, missing, or expired."""
    # token must be a bare filename ending in .csv — reject anything with
    # path separators or non-csv extensions
    if not token or '/' in token or '\\' in token or not token.endswith('.csv'):
        return None
    path = os.path.join(_cn_preview_dir(), token)
    if not os.path.isfile(path):
        return None
    age = datetime.now().timestamp() - os.path.getmtime(path)
    if age > _CN_PREVIEW_TTL_SEC:
        return None
    return path


@app.route('/import-credit-notes/preview', methods=['POST'])
def import_credit_notes_preview():
    if session.get('role') not in ('admin', 'manager'):
        flash('ต้องเป็น Admin หรือ Manager', 'danger')
        return redirect(url_for('payment_status'))
    f = request.files.get('cn_file')
    if not f or not f.filename.endswith('.csv'):
        flash('กรุณาเลือกไฟล์ .csv', 'danger')
        return redirect(url_for('payment_status'))

    import uuid
    from import_credit_notes import preview_credit_notes_import

    token = f'{uuid.uuid4().hex}.csv'
    tmp_path = os.path.join(_cn_preview_dir(), token)
    f.save(tmp_path)

    try:
        preview = preview_credit_notes_import(tmp_path)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        flash(f'อ่านไฟล์ไม่สำเร็จ: {exc}', 'danger')
        return redirect(url_for('payment_status'))

    return render_template(
        'import_cn_preview.html',
        preview=preview,
        token=token,
        original_filename=f.filename,
    )


@app.route('/import-credit-notes/commit', methods=['POST'])
def import_credit_notes_commit():
    if session.get('role') not in ('admin', 'manager'):
        flash('ต้องเป็น Admin หรือ Manager', 'danger')
        return redirect(url_for('payment_status'))
    token = (request.form.get('token') or '').strip()
    path = _cn_preview_path(token)
    if path is None:
        flash('ไฟล์ที่ตรวจสอบหมดอายุ — กรุณาอัปโหลดใหม่', 'warning')
        return redirect(url_for('payment_status'))

    from import_credit_notes import import_credit_notes as _do_import

    try:
        result = _do_import(path)
    except Exception as exc:
        flash(f'นำเข้าไม่สำเร็จ: {exc}', 'danger')
        return redirect(url_for('payment_status'))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    cna = result.get('credit_note_amounts') or {}
    flash(
        f'นำเข้าใบลดหนี้สำเร็จ — '
        f'backfilled ref {result.get("refs_backfilled", 0)} ใบ  |  '
        f'credit_note_amounts upsert {cna.get("upserted", 0)} รายการ  |  '
        f'ใหม่ {result.get("new_recorded", 0)} ใบ  |  '
        f'ข้าม {result.get("already_new", 0) + result.get("skipped", 0)} ใบ',
        'success'
    )
    return redirect(url_for('payment_status'))


# ── Template filters ──────────────────────────────────────────────────────────

@app.template_filter('fmt_price')
def fmt_price(v):
    if v is None:
        return '-'
    return f'{v:,.2f}'


@app.template_filter('fmt_qty')
def fmt_qty(v):
    if v is None:
        return '-'
    return f'{v:,}'


@app.template_filter('from_json')
def from_json(v):
    """Parse a JSON string into a Python value for in-template iteration.

    Returns None for empty input or invalid JSON, so templates can use
    `{% if … %}` guards naturally.
    """
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


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


# ── Product Conversions (สูตรแปลงสินค้า) ─────────────────────────────────────

@app.route('/conversions')
def conversion_list():
    formulas = models.get_conversion_formulas()
    recent_runs = models.get_recent_conversion_runs(limit=5)
    return render_template('conversions/list.html',
                           formulas=formulas, recent_runs=recent_runs)


@app.route('/conversions/history')
def conversion_history():
    runs = models.get_recent_conversion_runs(limit=200)
    return render_template('conversions/history.html', runs=runs)


def _get_active_products():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, sku, product_name, unit_type FROM products WHERE is_active=1 ORDER BY product_name"
    ).fetchall()
    conn.close()
    return rows


@app.route('/conversions/new', methods=['GET', 'POST'])
def conversion_new():
    if not session.get('role'):
        abort(403)
    products = _get_active_products()
    if request.method == 'POST':
        name              = request.form.get('name', '').strip()
        output_product_id = request.form.get('output_product_id', '').strip()
        output_qty        = request.form.get('output_qty', '1').strip()
        note              = request.form.get('note', '').strip()
        input_pids        = request.form.getlist('input_product_id[]')
        input_qtys        = request.form.getlist('input_quantity[]')

        inputs = [{'product_id': int(p), 'quantity': int(q)}
                  for p, q in zip(input_pids, input_qtys) if p and q]
        if not name or not output_product_id or not inputs:
            flash('กรุณากรอกชื่อสูตร สินค้าที่ได้ และวัตถุดิบอย่างน้อย 1 รายการ', 'danger')
            return render_template('conversions/form.html', products=products, formula=None, inputs=[])

        models.create_conversion_formula(
            name, int(output_product_id), int(output_qty), inputs, note
        )
        flash(f'สร้างสูตร "{name}" สำเร็จ', 'success')
        return redirect(url_for('conversion_list'))

    return render_template('conversions/form.html', products=products, formula=None, inputs=[])


@app.route('/conversions/<int:formula_id>/edit', methods=['GET', 'POST'])
def conversion_edit(formula_id):
    if not session.get('role'):
        abort(403)
    formula, inputs = models.get_conversion_formula(formula_id)
    if not formula:
        abort(404)
    products = _get_active_products()
    if request.method == 'POST':
        name              = request.form.get('name', '').strip()
        output_product_id = request.form.get('output_product_id', '').strip()
        output_qty        = request.form.get('output_qty', '1').strip()
        note              = request.form.get('note', '').strip()
        input_pids        = request.form.getlist('input_product_id[]')
        input_qtys        = request.form.getlist('input_quantity[]')

        new_inputs = [{'product_id': int(p), 'quantity': int(q)}
                      for p, q in zip(input_pids, input_qtys) if p and q]
        if not name or not output_product_id or not new_inputs:
            flash('กรุณากรอกข้อมูลให้ครบ', 'danger')
            return render_template('conversions/form.html', products=products, formula=formula, inputs=inputs)

        models.update_conversion_formula(
            formula_id, name, int(output_product_id), int(output_qty), new_inputs, note
        )
        flash(f'อัปเดตสูตร "{name}" สำเร็จ', 'success')
        return redirect(url_for('conversion_list'))

    return render_template('conversions/form.html', products=products, formula=formula, inputs=inputs)


@app.route('/conversions/<int:formula_id>/run', methods=['GET', 'POST'])
def conversion_run(formula_id):
    formula, inputs = models.get_conversion_formula(formula_id)
    if not formula or not formula['is_active']:
        abort(404)
    if request.method == 'POST':
        if not session.get('role'):
            abort(403)
        try:
            multiplier   = max(1, int(request.form.get('multiplier', 1)))
        except (ValueError, TypeError):
            multiplier   = 1
        reference_no = request.form.get('reference_no', '').strip()
        extra_note   = request.form.get('note', '').strip()

        success, message, _ = models.run_conversion(formula_id, multiplier, reference_no, extra_note)
        flash(message, 'success' if success else 'danger')
        if success:
            return redirect(url_for('conversion_list'))

    return render_template('conversions/run.html', formula=formula, inputs=inputs)


@app.route('/conversions/<int:formula_id>/delete', methods=['POST'])
def conversion_delete(formula_id):
    if not session.get('role'):
        abort(403)
    models.delete_conversion_formula(formula_id)
    flash('ลบสูตรเรียบร้อยแล้ว', 'success')
    return redirect(url_for('conversion_list'))


@app.route('/conversions/<int:formula_id>/deactivate', methods=['POST'])
def conversion_deactivate(formula_id):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    conn.execute("UPDATE conversion_formulas SET is_active=0 WHERE id=?", (formula_id,))
    conn.commit()
    conn.close()
    flash('ปิดใช้งานสูตรแล้ว', 'success')
    return redirect(url_for('conversion_list'))


@app.route('/conversions/<int:formula_id>/activate', methods=['POST'])
def conversion_activate(formula_id):
    if session.get('role') != 'admin':
        abort(403)
    conn = get_connection()
    conn.execute("UPDATE conversion_formulas SET is_active=1 WHERE id=?", (formula_id,))
    conn.commit()
    conn.close()
    flash('เปิดใช้งานสูตรแล้ว', 'success')
    return redirect(url_for('conversion_list'))


# ── Customer Map ──────────────────────────────────────────────────────────────

def _parse_bsn_customers():
    import re
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'data', 'source', 'bsn_customer_info.csv')
    with open(csv_path, encoding='cp874', errors='replace') as f:
        content = f.read()
    lines = [l.strip('"').replace('\xa0', ' ') for l in content.split('\n')]

    customers = []
    current_type = ''
    i = 0
    while i < len(lines):
        line = lines[i]
        type_match = re.match(r'\s+ประเภท\s*:\s*(.+)', line)
        if type_match:
            current_type = type_match.group(1).strip()
            i += 1; continue

        cust_match = re.match(r'  (\d{2}[ก-ฮA-Za-z]\d{2,3})\s+(.+?)\s{3,}(\S+)\s+(\S+)\s+\d+', line)
        if cust_match:
            code = cust_match.group(1)
            name = cust_match.group(2).strip()
            salesperson = cust_match.group(3)
            zone = cust_match.group(4)
            customer = {
                'code': code, 'name': name, 'salesperson': salesperson,
                'zone': zone, 'customer_type': current_type,
                'address': '', 'phone': '', 'tax_id': '',
                'credit_days': 0, 'contact': '',
            }
            addr_parts = []
            j = i + 1
            while j < len(lines) and j < i + 10:
                nl = lines[j]
                if re.match(r'  \d{2}[ก-ฮA-Za-z]\d{2,3}\s', nl): break
                if re.match(r'\(BSN\)', nl.strip()): j += 5; break
                am = re.match(r'\s+ที่อยู่\s*:\s*(.*?)\s+ผู้ติดต่อ\s*:\s*(.*)', nl)
                if am:
                    a = am.group(1).strip()
                    if a: addr_parts.append(a)
                    customer['contact'] = am.group(2).strip()
                elif re.match(r'\s{17,}[^\s]', nl):
                    a = re.sub(r'\s+เลขที่.*', '', re.sub(r'\s+เครดิต.*', '', nl)).strip()
                    if a and not a.startswith('(BSN)'): addr_parts.append(a)
                cm = re.search(r'เครดิต\s*:\s*(\d+)', nl)
                if cm: customer['credit_days'] = int(cm.group(1))
                pm = re.match(r'\s+โทร\.\s*:\s*(.*?)\s+เงื่อนไข', nl)
                if pm: customer['phone'] = pm.group(1).strip()
                tm = re.match(r'\s+Tax ID\s*:\s*(\d+)', nl)
                if tm: customer['tax_id'] = tm.group(1)
                j += 1
            customer['address'] = ' '.join(addr_parts)
            customers.append(customer)
            i = j; continue
        i += 1
    return customers


@app.route('/customers/map')
def customer_map():
    zone   = request.args.get('zone', '').strip()
    ctype  = request.args.get('type', '').strip()
    total, geocoded = models.get_geocode_progress()
    zones  = models.get_customer_zones()
    ctypes = models.get_customer_types()
    customers_json = models.get_customers_for_map(
        zone=zone or None, customer_type=ctype or None
    )
    return render_template('customer_map.html',
                           customers_json=customers_json,
                           zones=zones, ctypes=ctypes,
                           sel_zone=zone, sel_type=ctype,
                           total=total, geocoded=geocoded)


@app.route('/customers/import-bsn', methods=['POST'])
def customer_import_bsn():
    if session.get('role') != 'admin':
        abort(403)
    customers = _parse_bsn_customers()
    inserted, updated = models.import_customers_from_bsn(customers)
    flash(f'นำเข้าสำเร็จ: เพิ่มใหม่ {inserted} รายการ, อัปเดต {updated} รายการ', 'success')
    return redirect(url_for('customer_map'))


@app.route('/customers/geocode/<code>', methods=['POST'])
def customer_geocode(code):
    import urllib.request, urllib.parse, json as _json
    conn = get_connection()
    row = conn.execute("SELECT address, name FROM customers WHERE code=?", (code,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    address = row['address'] or row['name']
    query = urllib.parse.urlencode({'q': address + ' ประเทศไทย', 'format': 'json',
                                    'limit': 1, 'accept-language': 'th'})
    url = f'https://nominatim.openstreetmap.org/search?{query}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'SendaiBoonswat-ERP/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            models.save_customer_geocode(code, lat, lng)
            return jsonify({'ok': True, 'lat': lat, 'lng': lng, 'display': data[0].get('display_name','')})
        return jsonify({'ok': False, 'reason': 'no result'})
    except Exception as e:
        return jsonify({'ok': False, 'reason': str(e)}), 500


@app.route('/api/customers/geojson')
def customer_geojson():
    zone  = request.args.get('zone') or None
    ctype = request.args.get('type') or None
    rows  = models.get_customers_for_map(zone=zone, customer_type=ctype, geocoded_only=True)
    features = []
    for r in rows:
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [r['lng'], r['lat']]},
            'properties': {k: r[k] for k in ('code','name','zone','customer_type',
                                              'address','phone','salesperson','credit_days')}
        })
    return jsonify({'type': 'FeatureCollection', 'features': features})


# ── Labels (Q3 — print price tag / shelf label) ──────────────────────────────

@app.route('/labels')
def labels_view():
    if session.get('role') != 'admin':
        abort(404)
    return render_template('labels/index.html')


@app.route('/api/products/search')
def api_products_search():
    q = (request.args.get('q') or '').strip()
    limit = min(int(request.args.get('limit', 20)), 50)
    if not q:
        return jsonify({'items': []})
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT p.id, p.sku, p.product_name, p.base_sell_price, p.unit_type,
               (SELECT barcode FROM product_barcodes pb
                  WHERE pb.product_id = p.id
                  ORDER BY pb.is_primary DESC, pb.id ASC LIMIT 1) AS barcode
          FROM products p
         WHERE p.is_active = 1
           AND (p.product_name LIKE :q
                OR CAST(p.sku AS TEXT) LIKE :q
                OR EXISTS (SELECT 1 FROM product_barcodes pb
                            WHERE pb.product_id = p.id AND pb.barcode LIKE :q))
         ORDER BY
             CASE WHEN CAST(p.sku AS TEXT) = :exact THEN 0
                  WHEN p.product_name LIKE :starts THEN 1
                  ELSE 2 END,
             p.product_name
         LIMIT :lim
        """,
        {'q': f'%{q}%', 'starts': f'{q}%', 'exact': q, 'lim': limit}
    ).fetchall()
    conn.close()
    items = [{
        'id':         r['id'],
        'sku':        r['sku'],
        'name':       r['product_name'],
        'price':      r['base_sell_price'],
        'unit':       r['unit_type'],
        'barcode':    r['barcode'] or '',
    } for r in rows]
    return jsonify({'items': items})


@app.route('/api/products/<int:product_id>/barcodes', methods=['GET', 'POST', 'DELETE'])
def api_product_barcodes(product_id):
    if not session.get('role'):
        abort(403)
    conn = get_connection()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        barcode = (data.get('barcode') or '').strip()
        if not barcode:
            conn.close()
            return jsonify({'error': 'barcode required'}), 400
        try:
            conn.execute(
                "INSERT INTO product_barcodes (product_id, barcode, source) "
                "VALUES (?, ?, 'manual')",
                (product_id, barcode)
            )
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({'error': str(e)}), 400
    elif request.method == 'DELETE':
        bc_id = request.args.get('id')
        if bc_id:
            conn.execute("DELETE FROM product_barcodes WHERE id=? AND product_id=?",
                         (bc_id, product_id))
            conn.commit()
    rows = conn.execute(
        "SELECT id, barcode, is_primary, source FROM product_barcodes "
        "WHERE product_id=? ORDER BY is_primary DESC, id ASC",
        (product_id,)
    ).fetchall()
    conn.close()
    return jsonify({'items': [dict(r) for r in rows]})


# ── Commission / Express AR-AP dashboards ───────────────────────────────────
import commission as commission_mod  # noqa: E402
import hr as hr_mod  # noqa: E402  (referenced by blueprints/hr.py via direct import; kept here for parity with commission pattern)

# Make import_express's machinery available to the upload form. We inject
# our own DB connection so the import shares this app's transaction
# semantics (lights-on FK off etc).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import import_express as express_importer  # noqa: E402


def _months_with_payment_activity():
    """Distinct YYYY-MM strings present in express_payments_in (non-void)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT substr(date_iso, 1, 7) AS ym "
        "FROM express_payments_in WHERE is_void=0 ORDER BY ym DESC"
    ).fetchall()
    conn.close()
    return [r['ym'] for r in rows]


@app.route('/commission')
def commission_dashboard():
    months = _months_with_payment_activity()
    if not months:
        return render_template('commission.html', rows=[], months=[], year_month='',
                               summary={}, salespersons={})

    year_month = request.args.get('month') or months[0]
    rows = commission_mod.get_commission_for_month(year_month)

    # Show all 12 salespersons even if no activity, so dashboard is stable.
    conn = get_connection()
    sp_rows = conn.execute(
        "SELECT s.code, s.name, t.code AS tier_code "
        "FROM salespersons s "
        "LEFT JOIN commission_assignments a ON a.salesperson_code = s.code "
        "LEFT JOIN commission_tiers t ON t.id = a.tier_id "
        "ORDER BY s.code"
    ).fetchall()
    conn.close()
    sp_meta = {r['code']: dict(r) for r in sp_rows}

    activity = {r['salesperson_code']: r for r in rows}
    full_rows = []
    for code, meta in sp_meta.items():
        if code in activity:
            r = activity[code]
            r['salesperson_name'] = meta['name']
            full_rows.append(r)
        else:
            full_rows.append({
                'salesperson_code': code, 'salesperson_name': meta['name'],
                'tier_code': meta['tier_code'] or '?', 'tier_name': '',
                'own_net': 0.0, 'third_net': 0.0, 'total_net': 0.0,
                'threshold_amount': None,
                'commission_below': 0.0, 'commission_above_own': 0.0,
                'commission_above_third': 0.0, 'total_commission': 0.0,
                'receipts_count': 0, 'invoices_seen': 0, 'lines_attributed': 0,
            })
    full_rows.sort(key=lambda r: -r['total_net'])

    # Layer in paid-amount per salesperson for the month + cumulative
    # remaining (matches the drilldown's "ค้างจ่าย ถึง..." view, per
    # Put 2026-05-02). paid_amount stays month-only (= what we paid in
    # this cycle), remaining becomes cumulative through this month.
    paid_map = commission_mod.get_payouts_for_month(year_month)
    for r in full_rows:
        paid = paid_map.get(r['salesperson_code'], 0.0)
        r['paid_amount'] = paid
        # Cumulative unpaid through end of this month (mirrors drilldown)
        unpaid = commission_mod.get_invoice_commission_for_sp(
            year_month, r['salesperson_code'],
            through_month=True, only_unpaid=True)
        r['remaining'] = round(sum(i['remaining'] for i in unpaid), 2)
        if r['remaining'] <= 0.05:
            if r['total_commission'] and r['total_commission'] > 0:
                r['payout_status'] = 'paid'
            else:
                r['payout_status'] = 'none'
        elif paid > 0:
            r['payout_status'] = 'partial'
        else:
            r['payout_status'] = 'pending'

    summary = {
        'total_collected_net': sum(r['total_net'] for r in full_rows),
        'total_commission':    sum(r['total_commission'] for r in full_rows),
        'total_paid':          sum(r['paid_amount'] for r in full_rows),
        'total_remaining':     sum(r['remaining'] for r in full_rows),
        'breached_threshold':  sum(1 for r in full_rows
                                   if r['threshold_amount']
                                   and r['total_net'] > r['threshold_amount']),
    }
    today = date.today().isoformat()
    return render_template('commission.html',
                           rows=full_rows, months=months, year_month=year_month,
                           summary=summary, today=today)


@app.route('/commission/payout', methods=['POST'])
def commission_record_payout():
    """Record commission payouts.

    Two modes:
    1. Bulk per-invoice — form has invoice_no[] checkbox values, plus a
       hidden sp_code (one salesperson at a time). Used by the drill-down
       "tick invoices to mark paid" form. amount per invoice = remaining
       commission_due (computed by engine, sent as amount_<invoice>).
    2. Bulk per-salesperson — form has sp_code[] checkbox values, plus
       per-sp amount field amount_<sp>. Used by the /commission month
       overview (legacy form, still supported for whole-month payouts
       without per-invoice tracking).
    """
    year_month  = request.form.get('month', '').strip()
    paid_date   = request.form.get('paid_date') or date.today().isoformat()
    paid_method = request.form.get('paid_method', '').strip()
    note        = request.form.get('note', '').strip()
    paid_by     = session.get('username', '')
    redirect_to = request.form.get('redirect_to') or url_for('commission_dashboard',
                                                              month=year_month)

    # Mode 1: per-invoice tick-list
    inv_list = request.form.getlist('invoice_no')
    if inv_list:
        sp_code = request.form.get('sp_code', '').strip()
        if not sp_code:
            flash('ขาด sp_code', 'danger')
            return redirect(redirect_to)
        inserted = 0
        for inv in inv_list:
            amt_raw = request.form.get(f'amount_{inv}', '').strip()
            if not amt_raw:
                continue
            try:
                amt = float(amt_raw.replace(',', ''))
            except ValueError:
                continue
            if amt <= 0:
                continue
            commission_mod.record_payout(
                year_month=year_month, salesperson_code=sp_code,
                amount_paid=amt, paid_date=paid_date,
                paid_method=paid_method, note=note, paid_by=paid_by,
                invoice_no=inv,
            )
            inserted += 1
        if inserted:
            flash(f'บันทึกการจ่าย commission แล้ว {inserted} ใบ', 'success')
        else:
            flash('ไม่ได้บันทึก (ยอดเป็น 0 หรือว่างเปล่า)', 'warning')
        return redirect(redirect_to)

    # Mode 2: per-salesperson (legacy month overview form)
    sp_codes = request.form.getlist('sp_code')
    if not sp_codes:
        single = request.form.get('sp_code')
        if single:
            sp_codes = [single]
    inserted = 0
    for sp in sp_codes:
        amt_raw = request.form.get(f'amount_{sp}', '').strip() \
                  or request.form.get('amount', '').strip()
        if not amt_raw:
            continue
        try:
            amt = float(amt_raw.replace(',', ''))
        except ValueError:
            continue
        if amt <= 0:
            continue
        commission_mod.record_payout(
            year_month=year_month, salesperson_code=sp,
            amount_paid=amt, paid_date=paid_date,
            paid_method=paid_method, note=note, paid_by=paid_by,
        )
        inserted += 1
    if inserted:
        flash(f'บันทึกการจ่าย commission แล้ว {inserted} รายการ', 'success')
    else:
        flash('ไม่ได้บันทึก (เลือกจำนวน + ยอดให้ถูก)', 'warning')
    return redirect(redirect_to)


@app.route('/commission/payout/<int:payout_id>/delete', methods=['POST'])
def commission_delete_payout(payout_id):
    conn = get_connection()
    row = conn.execute(
        'SELECT year_month FROM commission_payouts WHERE id = ?',
        (payout_id,)
    ).fetchone()
    conn.close()
    commission_mod.delete_payout(payout_id)
    flash('ลบรายการจ่ายแล้ว', 'success')
    if row:
        return redirect(url_for('commission_payouts_list', month=row['year_month']))
    return redirect(url_for('commission_payouts_list'))


@app.route('/commission/payouts')
def commission_payouts_list():
    year_month = request.args.get('month', '').strip()
    sp_code = request.args.get('sp', '').strip()
    payouts = commission_mod.get_payout_history(
        year_month=year_month or None, salesperson_code=sp_code or None
    )
    months = _months_with_payment_activity()
    conn = get_connection()
    sp_rows = conn.execute('SELECT code, name FROM salespersons ORDER BY code').fetchall()
    conn.close()
    return render_template('commission_payouts.html',
                           payouts=payouts,
                           year_month=year_month, sp_code=sp_code,
                           months=months,
                           salespersons=[dict(r) for r in sp_rows],
                           total=sum(p['amount_paid'] for p in payouts))


@app.route('/commission/sp/<sp_code>/invoice/<invoice_no>')
def commission_invoice_detail(sp_code, invoice_no):
    year_month = request.args.get('month', '').strip()
    if not year_month:
        months = _months_with_payment_activity()
        year_month = months[0] if months else ''
    header, lines = commission_mod.get_invoice_line_breakdown(
        year_month, sp_code, invoice_no)
    conn = get_connection()
    sp_row = conn.execute('SELECT name FROM salespersons WHERE code = ?',
                          (sp_code,)).fetchone()
    conn.close()
    sp_name = sp_row['name'] if sp_row else sp_code
    return render_template('commission_invoice_detail.html',
                           sp_code=sp_code, sp_name=sp_name,
                           year_month=year_month,
                           header=header, lines=lines)


@app.route('/commission/sp/<sp_code>')
def commission_drilldown(sp_code):
    months = _months_with_payment_activity()
    year_month = request.args.get('month') or (months[0] if months else '')
    if not year_month:
        return render_template('commission_drilldown.html',
                               sp_code=sp_code, sp_name=sp_code, year_month='',
                               lines=[], invoices=[], months=months, summary=None)
    lines = commission_mod.get_lines_for_salesperson(year_month, sp_code)
    summary_rows = commission_mod.get_commission_for_month(year_month, sp_code)
    summary = summary_rows[0] if summary_rows else None
    # Group lines by invoice for nicer display
    inv_map = {}
    for ln in lines:
        inv = inv_map.setdefault(ln['invoice_no'], {
            'invoice_no': ln['invoice_no'],
            'receipt_no': ln['receipt_no'],
            'receipt_date': ln['receipt_date'],
            'customer_name': ln['customer_name'],
            'lines': [],
            'own_net': 0.0,
            'third_net': 0.0,
        })
        inv['lines'].append(ln)
        if ln['brand_kind'] == 'own':
            inv['own_net'] += ln['line_net'] or 0
        else:
            inv['third_net'] += ln['line_net'] or 0
    invoices = sorted(inv_map.values(),
                      key=lambda i: (i['receipt_date'] or '', i['invoice_no']),
                      reverse=True)
    conn = get_connection()
    sp_row = conn.execute('SELECT name FROM salespersons WHERE code = ?',
                          (sp_code,)).fetchone()
    conn.close()
    sp_name = sp_row['name'] if sp_row else sp_code
    # Per-invoice commission for the "tick to mark paid" workflow.
    # Through-month + only-unpaid: on the drill-down, picking month X
    # shows EVERY unpaid invoice with receipt-date ≤ end of X (carryover
    # included). Invoices already fully paid are hidden — Put doesn't
    # need to see them when settling.
    invoice_commissions = commission_mod.get_invoice_commission_for_sp(
        year_month, sp_code, through_month=True, only_unpaid=True)
    # Cumulative-remaining for the "คงเหลือ" summary card so it matches
    # what Put would see in the tick-list (Put 2026-05-02). Per-month
    # remaining was confusing because old-cycle carry-over wasn't
    # reflected.
    cumulative_remaining = sum(i['remaining'] for i in invoice_commissions)

    # Sort the per-invoice list per ?sort= and ?order= (default: receipt_date desc).
    sort_col = request.args.get('sort', 'receipt_date')
    sort_order = request.args.get('order', 'desc')
    SORT_KEYS = {
        'invoice_date':   lambda r: (r.get('invoice_date') or '', r['invoice_no']),
        'receipt_date':   lambda r: (r.get('receipt_date') or '', r['invoice_no']),
        'invoice_no':     lambda r: r['invoice_no'],
        'commission_due': lambda r: r['commission_due'],
    }
    keyfn = SORT_KEYS.get(sort_col, SORT_KEYS['receipt_date'])
    invoice_commissions.sort(key=keyfn, reverse=(sort_order == 'desc'))

    # All invoices issued in target month for this salesperson (paid + unpaid)
    all_invoices = commission_mod.get_invoices_for_salesperson(year_month, sp_code)
    payouts = commission_mod.get_payout_history(year_month=year_month,
                                                salesperson_code=sp_code)
    paid_amount = sum(p['amount_paid'] for p in payouts)
    return render_template('commission_drilldown.html',
                           sp_code=sp_code, sp_name=sp_name,
                           year_month=year_month, months=months,
                           invoices=invoices, summary=summary,
                           invoice_commissions=invoice_commissions,
                           all_invoices=all_invoices,
                           payouts=payouts,
                           paid_amount=paid_amount,
                           cumulative_remaining=cumulative_remaining,
                           sort_col=sort_col, sort_order=sort_order,
                           today=date.today().isoformat())


@app.route('/commission/export')
def commission_export():
    year_month = request.args.get('month') or ''
    if not year_month:
        abort(400)
    rows = commission_mod.get_commission_for_month(year_month)
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['salesperson_code', 'tier', 'own_net', 'third_net', 'total_net',
                'threshold', 'commission_below', 'commission_above_own',
                'commission_above_third', 'total_commission',
                'receipts', 'invoices', 'lines'])
    for r in rows:
        w.writerow([r['salesperson_code'], r['tier_code'],
                    f"{r['own_net']:.2f}", f"{r['third_net']:.2f}",
                    f"{r['total_net']:.2f}", r['threshold_amount'] or '',
                    f"{r['commission_below']:.2f}",
                    f"{r['commission_above_own']:.2f}",
                    f"{r['commission_above_third']:.2f}",
                    f"{r['total_commission']:.2f}",
                    r['receipts_count'], r['invoices_seen'], r['lines_attributed']])
    out = buf.getvalue().encode('utf-8-sig')  # BOM for Excel-Thai
    return send_file(io.BytesIO(out), mimetype='text/csv',
                     as_attachment=True,
                     download_name=f'commission_{year_month}.csv')


# ── Regions admin (fill in name_th + sort_order) ─────────────────────────────

@app.route('/regions', methods=['GET', 'POST'])
def regions_admin():
    if session.get('role') != 'admin':
        abort(403)

    if request.method == 'POST':
        try:
            region_id = int(request.form.get('region_id', '0'))
        except ValueError:
            abort(400)
        result = models.update_region(
            region_id,
            request.form.get('name_th', ''),
            request.form.get('sort_order', ''),
            request.form.get('note', ''),
        )
        if result['ok']:
            flash(f'อัปเดต region #{region_id} เรียบร้อย', 'success')
        else:
            flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')
        return redirect(url_for('regions_admin'))

    return render_template('regions.html', regions=models.get_all_regions_with_counts())


# ── Commission Overrides (admin-only CRUD) ───────────────────────────────────
# Rules sit in commission_overrides; the engine caches them in-process
# (commission._OVERRIDES_CACHE), so every successful write here calls
# clear_override_cache() so /commission picks up the change without restart.

def _require_admin():
    if session.get('role') != 'admin':
        abort(403)


def _safe_clear_override_cache():
    """Refresh the in-process override cache after a write. Must not raise:
    a stale cache is recoverable on next process restart, a 500 after a
    successful DB write isn't."""
    try:
        commission_mod.clear_override_cache()
    except Exception as e:
        flash(f'บันทึกแล้ว แต่ refresh cache ล้มเหลว: {e}. รีสตาร์ท Sendy ถ้าค่ายังไม่อัปเดต',
              'warning')


@app.route('/commission/overrides')
def commission_overrides_list():
    _require_admin()
    rules = models.list_commission_overrides(active_only=False)
    return render_template('commission_overrides_list.html', rules=rules)


@app.route('/commission/overrides/new', methods=['GET', 'POST'])
def commission_overrides_new():
    _require_admin()
    if request.method == 'POST':
        result = models.create_commission_override(request.form)
        if result['ok']:
            _safe_clear_override_cache()
            flash(f'เพิ่ม rule #{result["id"]} เรียบร้อย', 'success')
            return redirect(url_for('commission_overrides_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_overrides_form.html',
        rule=None,
        form=request.form if request.method == 'POST' else None,
        products=models.get_products(per_page=10000)[0],
        brands=models.get_brands(),
        salespersons=models.get_active_salespersons(),
    )


@app.route('/commission/overrides/<int:override_id>/edit', methods=['GET', 'POST'])
def commission_overrides_edit(override_id):
    _require_admin()
    rule = models.get_commission_override(override_id)
    if not rule:
        abort(404)

    if request.method == 'POST':
        result = models.update_commission_override(override_id, request.form)
        if result['ok']:
            _safe_clear_override_cache()
            flash(f'อัปเดต rule #{override_id} เรียบร้อย', 'success')
            return redirect(url_for('commission_overrides_list'))
        flash(f'ไม่สามารถบันทึก: {result["error"]}', 'danger')

    return render_template(
        'commission_overrides_form.html',
        rule=rule,
        form=request.form if request.method == 'POST' else None,
        products=models.get_products(per_page=10000)[0],
        brands=models.get_brands(),
        salespersons=models.get_active_salespersons(),
    )


@app.route('/commission/overrides/<int:override_id>/toggle', methods=['POST'])
def commission_overrides_toggle(override_id):
    _require_admin()
    result = models.toggle_commission_override(override_id)
    if result['ok']:
        commission_mod.clear_override_cache()
        state = 'active' if result['is_active'] else 'inactive'
        flash(f'rule #{override_id} → {state}', 'success')
    else:
        flash(f'ไม่สามารถ toggle: {result["error"]}', 'danger')
    return redirect(url_for('commission_overrides_list'))


@app.route('/commission/overrides/<int:override_id>/delete', methods=['POST'])
def commission_overrides_delete(override_id):
    _require_admin()
    result = models.delete_commission_override(override_id)
    if result['ok']:
        commission_mod.clear_override_cache()
        flash(f'ลบ rule #{override_id} เรียบร้อย', 'success')
    else:
        flash(f'ไม่สามารถลบ: {result["error"]}', 'danger')
    return redirect(url_for('commission_overrides_list'))


@app.route('/express/import', methods=['GET', 'POST'])
def express_import():
    """Upload & import a weekly Express export."""
    if request.method == 'POST':
        file_type = request.form.get('file_type', '').strip()
        company_code = request.form.get('company', 'BSN').strip()
        # Default = incremental (skip doc_no already in DB) — safer for
        # repeated weekly uploads. Uncheck to force full re-import (may
        # create duplicates).
        incremental = bool(request.form.get('incremental'))
        upload = request.files.get('file')

        if file_type not in ('credit_notes', 'payments_in', 'ar_snapshot',
                             'ap_snapshot', 'payments_out', 'sales'):
            flash('เลือกประเภทไฟล์ไม่ถูก', 'danger')
            return redirect(url_for('express_import'))
        if not upload or not upload.filename:
            flash('ไม่ได้แนบไฟล์', 'danger')
            return redirect(url_for('express_import'))

        upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'express')
        os.makedirs(upload_dir, exist_ok=True)
        from datetime import datetime as _dt
        ts = _dt.now().strftime('%Y%m%d_%H%M%S')
        safe_name = f'{ts}_{file_type}_{upload.filename}'
        save_path = os.path.join(upload_dir, safe_name)
        upload.save(save_path)

        try:
            express_importer.run_import(file_type, save_path,
                                        company_code=company_code,
                                        dry_run=False,
                                        incremental=incremental)
            mode = 'incremental (ข้ามรายการซ้ำ)' if incremental else 'full (รวมรายการซ้ำ)'
            flash(f'นำเข้า {file_type} ({mode}) สำเร็จ — ไฟล์: {upload.filename}', 'success')
        except Exception as e:
            flash(f'นำเข้าไม่สำเร็จ: {e}', 'danger')
        return redirect(url_for('express_import'))

    # GET — list recent batches + show form
    conn = get_connection()
    batches = conn.execute("""
        SELECT id, file_type, source_filename, record_count, line_count,
               snapshot_date_iso, status, imported_at
          FROM express_import_log
         ORDER BY id DESC
         LIMIT 20
    """).fetchall()
    conn.close()
    return render_template('express_import.html',
                           batches=[dict(r) for r in batches])


@app.route('/express/ar')
def express_ar_dashboard():
    """AR outstanding view from the latest express_ar_outstanding snapshot."""
    conn = get_connection()
    snapshot = conn.execute(
        "SELECT MAX(snapshot_date_iso) AS d FROM express_ar_outstanding WHERE entity = 'BSN'"
    ).fetchone()
    snapshot_date = snapshot['d'] if snapshot else None

    search = (request.args.get('q') or '').strip()
    sp_filter = (request.args.get('sp') or '').strip()
    sort = request.args.get('sort', 'amount')

    # Exclude !RE "ใบรับชำระไม่เรียบร้อย" anomalies (Put 2026-05-02:
    # "RE ไม่ควรอยู่ในหน้า ar เพราะลูกหนี้จ่ายแล้ว"). These are legacy
    # 2005-2019 receipts that Express marks with is_anomalous=1; they
    # are not real outstanding debt.
    where = ["entity = 'BSN'", 'snapshot_date_iso = ?', 'is_anomalous = 0']
    params = [snapshot_date]
    if search:
        where.append("(customer_name LIKE ? OR customer_code LIKE ?)")
        params += [f'%{search}%', f'%{search}%']
    if sp_filter:
        where.append('salesperson_code = ?')
        params.append(sp_filter)

    order = {
        'amount': 'outstanding_amount DESC',
        'date':   'doc_date_iso ASC',
        'customer': 'customer_name ASC, doc_date_iso ASC',
    }.get(sort, 'outstanding_amount DESC')

    rows = conn.execute(f"""
        SELECT customer_code, customer_name, customer_type, salesperson_code,
               doc_no, doc_date_iso, bill_amount, paid_amount, outstanding_amount,
               is_anomalous, has_warning,
               CAST(julianday('now') - julianday(doc_date_iso) AS INTEGER) AS age_days
          FROM express_ar_outstanding
         WHERE {' AND '.join(where)}
         ORDER BY {order}
        LIMIT 1000
    """, params).fetchall()

    summary = conn.execute(
        f"SELECT COUNT(*) AS n_docs, COUNT(DISTINCT customer_code) AS n_customers, "
        f"ROUND(SUM(outstanding_amount), 2) AS total "
        f"FROM express_ar_outstanding WHERE {' AND '.join(where)}",
        params
    ).fetchone()

    sps = [r['salesperson_code'] for r in conn.execute(
        "SELECT DISTINCT salesperson_code FROM express_ar_outstanding "
        "WHERE snapshot_date_iso=? AND salesperson_code <> '' AND is_anomalous=0 "
        "ORDER BY salesperson_code", (snapshot_date,)
    ).fetchall()]
    conn.close()

    return render_template('express_ar.html',
                           rows=[dict(r) for r in rows],
                           summary=dict(summary) if summary else {},
                           snapshot_date=snapshot_date,
                           sps=sps, sp_filter=sp_filter,
                           search=search, sort=sort)


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

    # Pull recent payment history (เฉพาะลูกค้านี้)
    recent_payments = conn.execute("""
        SELECT pin.doc_no, pin.date_iso,
               pin.cash_amount, pin.cheque_amount, pin.discount_amount,
               pin.salesperson_code, pin.note
          FROM express_payments_in pin
         WHERE pin.is_void = 0
           AND pin.customer_id = ?
         ORDER BY pin.date_iso DESC
         LIMIT 20
    """, (customer_code,)).fetchall()
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
    """AP supplier-payment view from express_payments_out + AP outstanding snapshot."""
    conn = get_connection()
    date_from = request.args.get('from') or '2024-01-01'
    date_to   = request.args.get('to')   or date.today().isoformat()

    rows = conn.execute("""
        SELECT supplier_name,
               COUNT(*) AS payments,
               ROUND(SUM(invoice_amount), 2) AS invoice_total,
               ROUND(SUM(cash_amount + cheque_amount), 2) AS paid_total,
               ROUND(SUM(discount_amount), 2) AS discount_total,
               MAX(date_iso) AS last_paid
          FROM express_payments_out
         WHERE is_void = 0
           AND date_iso BETWEEN ? AND ?
         GROUP BY supplier_name
         ORDER BY paid_total DESC
    """, (date_from, date_to)).fetchall()

    summary = conn.execute("""
        SELECT COUNT(*) AS n_payments,
               COUNT(DISTINCT supplier_name) AS n_suppliers,
               ROUND(SUM(cash_amount + cheque_amount), 2) AS total_paid
          FROM express_payments_out
         WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
    """, (date_from, date_to)).fetchone()

    recent = conn.execute("""
        SELECT doc_no, date_iso, supplier_name, invoice_amount,
               (cash_amount + cheque_amount) AS paid, note
          FROM express_payments_out
         WHERE is_void = 0 AND date_iso BETWEEN ? AND ?
         ORDER BY date_iso DESC, doc_no DESC
         LIMIT 50
    """, (date_from, date_to)).fetchall()

    ap_outstanding = models.get_ap_outstanding(conn)
    conn.close()

    return render_template('express_ap.html',
                           rows=[dict(r) for r in rows],
                           recent=[dict(r) for r in recent],
                           summary=dict(summary) if summary else {},
                           date_from=date_from, date_to=date_to,
                           ap=ap_outstanding)


# ── Accounting Summary ────────────────────────────────────────────────────────

@app.route('/accounting')
def accounting_summary():
    """
    Accounting summary landing page for the 'การค้า & บัญชี' module.
    Admin + manager: full view including cost/margin.
    Staff: redirected — same gating as cost-visible pages (e.g. customer_summary).
    """
    if session.get('role') not in ('admin', 'manager'):
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


# ── Cash Flow Dashboard ────────────────────────────────────────────────────────

@app.route('/cashflow')
def cashflow_dashboard():
    """Cash flow dashboard: cash-in by RE month + AR aging + accrual revenue.

    Admin + manager only (same gating as accounting_summary).
    Optional ?from=YYYY-MM&to=YYYY-MM period filter.
    Default: last 12 months ending the latest data month.
    """
    if session.get('role') not in ('admin', 'manager'):
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
    if session.get('role') not in ('admin', 'manager'):
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
    if session.get('role') not in ('admin', 'manager'):
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
    if session.get('role') not in ('admin', 'manager'):
        flash('ต้องเข้าสู่ระบบด้วยบัญชี Admin หรือ Manager', 'danger')
        return redirect(url_for('dashboard'))
    return None


def _arf_require_admin():
    if session.get('role') != 'admin':
        flash('ต้องใช้บัญชี Admin', 'danger')
        return redirect(url_for('ar_followup'))
    return None


@app.route('/accounting/ar-followup')
def ar_followup():
    """Ranked AR follow-up workspace.

    Filters (query params):
      ?bucket=0-30|31-60|61-90|90+   — only show customers with $$ in that bucket
      ?min=<฿>                       — min outstanding per customer
      ?q=<text>                      — case-insensitive customer-name search
      ?sort=outstanding|age|count    — default outstanding
    """
    redirect_ = _arf_require_manager()
    if redirect_:
        return redirect_

    bucket   = request.args.get('bucket', '').strip()
    min_str  = request.args.get('min', '').strip()
    search   = request.args.get('q', '').strip()
    sort     = request.args.get('sort', 'outstanding')

    try:
        min_amt = float(min_str.replace(',', '')) if min_str else 0.0
    except ValueError:
        min_amt = 0.0

    rows = arf_mod.customer_ranking(min_outstanding=min_amt)

    if bucket in ('0-30', '31-60', '61-90', '90+'):
        rows = [r for r in rows if r['age_buckets'].get(bucket, 0) > 0]
    if search:
        s = search.lower()
        rows = [r for r in rows if s in (r['customer'] or '').lower()
                                or s in (r.get('customer_code') or '').lower()]
    if sort == 'age':
        rows.sort(key=lambda r: -r['oldest_age_days'])
    elif sort == 'count':
        rows.sort(key=lambda r: -r['invoice_count'])
    # else: already sorted by outstanding DESC

    aging = cf_mod.ar_aging()
    overdue = arf_mod.list_overdue_followups()

    return render_template(
        'ar_followup.html',
        rows=rows,
        aging=aging,
        overdue=overdue,
        bucket=bucket,
        min_str=min_str,
        search=search,
        sort=sort,
    )


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
        return redirect(url_for('ar_followup'))

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


@app.route('/accounting/ar-followup/log/<int:log_id>/edit', methods=['POST'])
def ar_followup_log_edit(log_id):
    redirect_ = _arf_require_admin()
    if redirect_:
        return redirect_

    customer_key = (request.form.get('customer_key') or '').strip()
    def _f(name):
        v = request.form.get(name, '').strip()
        return v or None

    promised_amount = _f('promised_amount')
    try:
        promised_amount = float(promised_amount.replace(',', '')) if promised_amount else None
    except ValueError:
        promised_amount = None

    try:
        arf_mod.update_outreach(
            log_id=log_id,
            log_date=_f('log_date'),
            channel=_f('channel'),
            contact_person=_f('contact_person'),
            result=_f('result'),
            promised_amount=promised_amount,
            promised_date=_f('promised_date'),
            next_action_date=_f('next_action_date'),
            notes=_f('notes'),
        )
        flash('อัปเดตแล้ว', 'success')
    except sqlite3.IntegrityError as e:
        flash(f'ข้อมูลไม่ถูกต้อง: {e}', 'danger')

    if customer_key:
        return redirect(url_for('ar_followup_customer', customer_key=customer_key))
    return redirect(url_for('ar_followup'))


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
    return redirect(url_for('ar_followup'))


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
