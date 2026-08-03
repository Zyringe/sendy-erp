"""book_registry — the two-connection contract (P2a).

Pins: default book = main DB; VAT connection is structurally read-only with
its own init (no WAL pragma); borrowed per-request lifetime (cached on g,
closed once at teardown); missing VAT file raises BookUnavailable; auth never
touches the book connection; os.replace under an open handle serves the old
generation to the old request and the new one to the next request."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import book_registry as br


def _flask_app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app


def _make_vat_file(path, built_at='2026-08-02T00:00:00'):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO book_meta VALUES ('built_at', ?)", (built_at,))
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()


def _vat_path():
    import config
    return br.book_db_path('vat')


def test_default_book_is_main_db_and_writable(tmp_db):
    app = _flask_app()
    with app.test_request_context('/'):
        conn = br.get_book_connection()
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0
        conn.execute("CREATE TEMP TABLE w (x)")   # main conn: not read-only


def test_outside_request_context_defaults_to_novat(tmp_db):
    assert br.active_book() == 'novat'


def test_vat_connection_is_structurally_read_only(tmp_db):
    _make_vat_file(_vat_path())
    app = _flask_app()
    with app.test_request_context('/'):
        from flask import session, g
        session['active_book'] = 'vat'
        conn = br.get_book_connection()
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
        assert g.book_meta['built_at'] == '2026-08-02T00:00:00'
        assert g.book_name == 'vat'
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (2)")
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_borrowed_connection_cached_then_closed_at_teardown(tmp_db):
    app = _flask_app()
    with app.test_request_context('/'):
        c1 = br.get_book_connection()
        c2 = br.get_book_connection()
        assert c1 is c2
    with pytest.raises(sqlite3.ProgrammingError):
        c1.execute("SELECT 1")                    # teardown closed it


def test_unknown_session_value_falls_back_to_novat(tmp_db):
    app = _flask_app()
    with app.test_request_context('/'):
        from flask import session
        session['active_book'] = 'hack'
        assert br.active_book() == 'novat'


def test_missing_vat_file_raises_book_unavailable(tmp_db):
    assert not os.path.exists(_vat_path())
    app = _flask_app()
    with app.test_request_context('/'):
        from flask import session
        session['active_book'] = 'vat'
        with pytest.raises(br.BookUnavailable):
            br.get_book_connection()


def test_auth_path_never_touches_book_connection(tmp_db):
    """/login renders fine with the session pointing at a NONEXISTENT vat
    book — proof the auth path runs entirely on the main connection."""
    app = _flask_app()
    client = app.test_client()
    with client.session_transaction() as s:
        s['active_book'] = 'vat'
    assert client.get('/login').status_code == 200


def test_replace_under_open_handle_keeps_generations_separate(tmp_db, tmp_path):
    _make_vat_file(_vat_path(), built_at='GEN1')
    app = _flask_app()
    ctx = app.test_request_context('/')
    ctx.push()
    try:
        from flask import session
        session['active_book'] = 'vat'
        conn = br.get_book_connection()
        assert conn.execute(
            "SELECT value FROM book_meta WHERE key='built_at'").fetchone()[0] == 'GEN1'
        # publish GEN2 exactly like the upload flow: build aside + os.replace
        newfile = tmp_path / 'vat_new.db'
        _make_vat_file(str(newfile), built_at='GEN2')
        os.replace(str(newfile), _vat_path())
        # the in-flight request keeps its inode
        assert conn.execute(
            "SELECT value FROM book_meta WHERE key='built_at'").fetchone()[0] == 'GEN1'
    finally:
        ctx.pop()
    with app.test_request_context('/'):
        from flask import session
        session['active_book'] = 'vat'
        conn2 = br.get_book_connection()
        assert conn2.execute(
            "SELECT value FROM book_meta WHERE key='built_at'").fetchone()[0] == 'GEN2'
