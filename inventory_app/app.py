"""Sendy ERP — Flask application core.

Post-refactor (2026-07, phases 1-12) this file is the ~350-line application
core. It owns ONLY:

- Flask app construction (`app = Flask(__name__)`) + registration of the 20
  blueprints in `blueprints/` + `init_access_control(app)` + filters
- The auth core, kept here permanently: /login, /logout, /healthz,
  service-worker + install help, bootstrap DB upload, /dashboard, and the
  admin act-as (impersonation) routes — wired into before_request literals,
  `_role_home`, and the CSRF error handler

Everything else lives elsewhere:
- Role/POST-permission gate + endpoint→module map: `access_control.py`
  (`_STAFF_POST_OK`, `_MANAGER_POST_OK`, `_ENDPOINT_MODULE`, `require_login`)
- Template filters: `filters.py`
- Domain routes: `blueprints/<domain>.py` (20 blueprints)
- Business logic + DB queries: the `models/` package (pure facade
  `__init__.py` + 22 domain submodules)

Permission model (source of truth: access_control.py):
  - admin: full access + user management
  - manager: see cost/GP/payments; cannot edit products/users
  - staff: import weekly flow + read-only views (no cost/GP, no hr.*,
    no cashbook.*)
"""
import io
import json
import os
import sys
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
                   flash, session, abort,
                   send_from_directory)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import models
from database import init_db, get_connection
from blueprints.products import bp_products
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
from blueprints.labels import bp_labels
from blueprints.inventory import bp_inventory
from blueprints.partners import bp_partners
from blueprints.sales import bp_sales
from blueprints.commission_bp import bp_commission
from blueprints.ecommerce import bp_ecommerce
from blueprints.accounting import bp_accounting
from blueprints.admin import bp_admin
# Re-exported below for app.py's own remaining code (_role_home) and
# for tests that import these off `app` (_STAFF_POST_OK, _MANAGER_POST_OK,
# _ENDPOINT_MODULE, _MODULE_DEFS, build_mobile_nav_slots).
from access_control import (_STAFF_POST_OK, _MANAGER_POST_OK, _ENDPOINT_MODULE,
                            _MODULE_DEFS, ROLE_ORDER, _role_home,
                            build_mobile_nav_slots, init_access_control,
                            pw_fingerprint)
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
app.register_blueprint(bp_labels)
app.register_blueprint(bp_inventory)
app.register_blueprint(bp_partners)
app.register_blueprint(bp_sales)
app.register_blueprint(bp_commission)
app.register_blueprint(bp_ecommerce)
app.register_blueprint(bp_accounting)
app.register_blueprint(bp_admin)

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
            session['pw_fp']        = pw_fingerprint(user['password_hash'])  # cross-session invalidation
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



# ── User management, admin DB upload/download, backups ── moved to
#    blueprints/admin.py ────────────────────────────────────────────────────────────


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
        return redirect(url_for('admin.user_list'))
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



# ── Temp: Download DB (ลบออกหลังใช้) ── moved to blueprints/admin.py ──────────────────────────

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


# ── _table_exists / _replace_master_tables / upload_db / upload_db_confirm /
#    backups (_backups_dir, _reload_workers_after_restore, backups_list,
#    backup_download, backup_restore, backup_delete) ── moved to
#    blueprints/admin.py ─────────────────────────────────────────────────────────


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
    express_freshness = models.get_express_dbf_freshness()
    return render_template('dashboard.html',
                           restock_count=restock_count,
                           recent_txns=recent_txns,
                           total_products=total_products,
                           in_stock_count=in_stock_count,
                           payroll_reminder=payroll_reminder,
                           express_freshness=express_freshness)


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


# ── E-commerce platform SKU sync + listing mapping — moved to blueprints/ecommerce.py ──
# ── Customer Map — moved to blueprints/partners.py ───────────────────────────


# ── Commission dashboard, payouts, drilldown, export, overrides — moved to
#    blueprints/commission_bp.py ────────────────────────────────────────────
import hr as hr_mod  # noqa: E402  (referenced by blueprints/hr.py via direct import)


# ── Regions admin — moved to blueprints/partners.py ──────────────────────────


# -- Express AR/AP redirects, unified AP/AR dashboards, accounting summary,
#    cash flow + revenue dashboards, AR follow-up workspace -- moved to
#    blueprints/accounting.py (registered via bp_accounting above) --------



if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)
