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
import os
import sqlite3

from flask import g, session, has_request_context, flash, redirect, url_for

import config
import database

DEFAULT_BOOK = 'novat'

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
    context (scripts, cron) and for any unknown session value."""
    if not has_request_context():
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
