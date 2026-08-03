"""Accounting-book registry + the book-scoped read connection.

Two explicit connection contracts (vat-book-view plan rev 3):

  database.get_connection()   → the MAIN DB, always. Auth, config, mappings,
                                uploads, every operational module. Unchanged.
  get_book_connection()       → book-dependent LEDGER/CATALOG reads only
                                (the parity set: sales, purchases, AR, AP,
                                products, stock). Honors the session toggle.

Ownership contract: get_book_connection() returns a BORROWED, request-scoped
connection cached on flask.g — callers must never close() it or wrap it in a
closing context. It is closed exactly once in the appcontext teardown. Helpers
that accept an optional conn (e.g. revenue._ConnCtx) receive it as the
caller's connection, which those helpers already know not to close.

The VAT book is a read-only artifact (vat_book.db, built by vat_book_builder
in a subprocess and atomically replaced by the upload flow). Its connection is
opened `mode=ro` + `PRAGMA query_only=ON` — writes are structurally impossible,
not policy-blocked. No WAL pragma is ever issued on it (the file is finalized
journal_mode=delete; a WAL switch would need write access). os.replace during
an in-flight request is safe: this request keeps reading its already-open
inode; the next request opens the new file.
"""
import json
import os
import sqlite3

from flask import (g, session, has_request_context, flash, redirect, url_for,
                   request, jsonify)

import config
import database

DEFAULT_BOOK = 'novat'

# Endpoints usable while VIEWING the VAT book (the parity set, page-centric:
# each page's XHR deps are listed too — see plan rev 3 P2b contract).
PARITY_ENDPOINTS = {
    'sales.trade_dashboard',
    'sales.sales_view', 'sales.sales_doc',
    'sales.purchases_view', 'sales.purchases_doc',
    'products.product_list', 'products.product_detail',
    'products.api_product_barcodes',      # GET only; unsafe methods denied below
    'inventory.transaction_history',
}

# Infra endpoints that must work regardless of the selected book.
INFRA_ENDPOINTS = {
    'login', 'logout', 'static', 'healthz', 'bootstrap_upload_db',
    'serve_sw', 'help_install', 'toggle_book',
}

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}

BOOKS = {
    'novat': {
        'label': 'สมุดปัจจุบัน (No-VAT)',
        'db_filename': None,          # None → the main DB
    },
    'vat': {
        'label': 'สมุด VAT (xp5)',
        'db_filename': 'vat_book.db',
        # ISINFO identity the upload flow requires before accepting a dataset
        # as this book (P3 classification — never guess by folder name).
        'isinfo_signature': {'TAXID': '0105542067386',
                             'THINAM': 'บริษัท บุญสวัสดิ์ นำชัย จำกัด'},
    },
}


class BookUnavailable(Exception):
    """The selected book's DB file does not exist yet (never built/uploaded)."""


def book_db_path(book):
    fname = BOOKS[book]['db_filename']
    if fname is None:
        return config.DATABASE_PATH
    return os.path.join(os.path.dirname(config.DATABASE_PATH), fname)


def active_book():
    """The session's selected book, validated; 'novat' outside a request
    context (scripts, cron), for any unknown session value, and ALWAYS for
    the 'general' kiosk role — its only allowed page (/m/stock) is outside
    the parity set, so a stuck VAT session would redirect-loop it against
    access_control's GET allowlist (Codex R4 P1)."""
    if not has_request_context():
        return DEFAULT_BOOK
    if session.get('role') == 'general':
        return DEFAULT_BOOK
    book = session.get('active_book', DEFAULT_BOOK)
    return book if book in BOOKS else DEFAULT_BOOK


def get_book_connection():
    """Borrowed, request-scoped connection for the ACTIVE book (see module
    docstring). The book choice and (for the VAT book) the build generation
    are captured once per request, at first use."""
    if 'book_conn' not in g:
        book = active_book()
        if BOOKS[book]['db_filename'] is None:
            conn = database.get_connection()
        else:
            path = book_db_path(book)
            if not os.path.exists(path):
                raise BookUnavailable(book)
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True,
                                   check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
        g.book_conn = conn
        g.book_name = book
        g.book_meta = _read_book_meta(conn) if book != DEFAULT_BOOK else {}
    return g.book_conn


def _read_book_meta(conn):
    try:
        return dict(conn.execute("SELECT key, value FROM book_meta"))
    except sqlite3.Error:
        return {}


def vat_book_freshness():
    """book_meta of the PUBLISHED vat_book.db, or None if never built.
    Short-lived ro connection — main-book pages (the import screen) show
    this, so it deliberately does not touch the g-cached book connection."""
    path = book_db_path('vat')
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            meta = dict(conn.execute("SELECT key, value FROM book_meta"))
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if 'counts' in meta:
        try:
            meta['counts'] = json.loads(meta['counts'])
        except ValueError:
            pass
    return meta


def _wants_json():
    return (request.path.startswith('/api/')
            or request.headers.get('X-Expected-Book') is not None
            or 'application/json' in (request.headers.get('Accept') or ''))


def _expected_book_from_request():
    """The book the submitting page was RENDERED under (stale-tab binding).
    Header first (fetch writes); then the injected hidden form field — but only
    for urlencoded bodies, so the guard never force-parses a large multipart
    upload ahead of the route's own size checks."""
    exp = request.headers.get('X-Expected-Book')
    if exp:
        return exp
    if request.mimetype == 'application/x-www-form-urlencoded':
        return request.form.get('expected_book')
    return None


def init_book_registry(app):
    @app.teardown_appcontext
    def _close_book_conn(exc):
        conn = g.pop('book_conn', None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @app.errorhandler(BookUnavailable)
    def _book_unavailable(exc):
        # Selecting a book whose file was never built must not strand the
        # user: drop back to the default book loudly.
        session['active_book'] = DEFAULT_BOOK
        flash('สมุด VAT ยังไม่ถูกสร้าง — อัปโหลดชุดข้อมูล xp5 ที่หน้า Import ก่อนค่ะ',
              'warning')
        return redirect(url_for('dashboard'))

    @app.post('/book/toggle')
    def toggle_book():
        if session.get('role') == 'general':
            # kiosk role: no book concept (see active_book) — belt for the
            # POST-whitelist suspenders in access_control.
            session['active_book'] = DEFAULT_BOOK
            return redirect(url_for('mobile.stock_search'))
        target = request.form.get('book', DEFAULT_BOOK)
        if target not in BOOKS:
            target = DEFAULT_BOOK
        if target != DEFAULT_BOOK and not os.path.exists(book_db_path(target)):
            flash('สมุด VAT ยังไม่ถูกสร้าง — อัปโหลดชุดข้อมูล xp5 ที่หน้า Import ก่อนค่ะ',
                  'warning')
            return redirect(request.referrer or url_for('dashboard'))
        session['active_book'] = target
        if target == DEFAULT_BOOK:
            return redirect(url_for('dashboard'))
        # Land on a parity page — the dashboard is blocked in VAT mode.
        return redirect(url_for('sales.sales_view'))

    @app.before_request
    def _book_guard():
        ep = request.endpoint or ''
        book = active_book()

        # Infra endpoints are book-agnostic: auth flows and the toggle itself
        # must work from ANY rendered page (the toggle form legitimately
        # carries expected_book='vat' when leaving the VAT view).
        if ep in INFRA_ENDPOINTS:
            return

        # Stale-tab binding (Codex R2 F1): an unsafe request carries the book
        # its page was rendered under. Reject when the rendering book was the
        # VAT book (read-only pages never submit) or differs from the session
        # book (another tab switched books after this page rendered) — a stale
        # submit must never write cross-book. Absent field (legacy/no-JS/
        # multipart) falls through to the session-book guard below.
        if request.method not in SAFE_METHODS:
            exp = _expected_book_from_request()
            if exp is not None and (exp != DEFAULT_BOOK or exp != book):
                msg = ('สมุด VAT อ่านอย่างเดียว — บันทึกไม่ได้ค่ะ'
                       if exp != DEFAULT_BOOK else
                       'หน้านี้ถูกเปิดไว้จากสมุดอื่น — กดรีเฟรชหน้านี้ก่อนบันทึกค่ะ')
                if _wants_json():
                    return jsonify({'error': msg, 'expected_book': exp,
                                    'active_book': book}), 409
                flash(msg, 'warning')
                return redirect(request.referrer or url_for('dashboard'))

        if book == DEFAULT_BOOK:
            return

        # VAT mode is read-only: no unsafe method reaches ANY route.
        if request.method not in SAFE_METHODS:
            msg = 'สมุด VAT อ่านอย่างเดียว — สลับกลับสมุดปัจจุบันก่อนแก้ไขค่ะ'
            if _wants_json():
                return jsonify({'error': msg}), 403
            flash(msg, 'warning')
            return redirect(url_for('sales.sales_view'))

        # Only the parity set renders the VAT book; everything else is
        # explicitly unavailable (never empty-rendered, never main-book data).
        if ep not in PARITY_ENDPOINTS:
            msg = 'หน้านี้ใช้ได้เฉพาะสมุดปัจจุบัน — สมุด VAT เปิดเฉพาะ ขาย/ซื้อ/สินค้า/สต็อกค่ะ'
            if _wants_json():
                return jsonify({'error': msg}), 403
            flash(msg, 'warning')
            return redirect(url_for('sales.sales_view'))

    @app.context_processor
    def _book_context():
        book = active_book() if has_request_context() else DEFAULT_BOOK
        ctx = {'active_book': book,
               'active_book_label': BOOKS[book]['label'],
               'vat_book_available': os.path.exists(book_db_path('vat')),
               'vat_book_meta': None}
        if book != DEFAULT_BOOK and ctx['vat_book_available']:
            try:
                get_book_connection()
                ctx['vat_book_meta'] = g.get('book_meta') or {}
            except BookUnavailable:      # race: file vanished after the check
                pass
        return ctx
