"""Route-level integration tests for bp_review (ตรวจบิล review UI).

Covers:
  - Anonymous → login redirect (GET and POST)
  - GET /review → redirect to latest batch or batches list
  - GET /review/batches → 200
  - GET /review/batch/<id> with seeded data → 200, shows doc_base
  - Staff POST mark_doc → in _STAFF_POST_OK (passes whitelist)
  - POST status=wrong without note → rejected with Thai message
  - POST status=wrong with note → OK
  - POST status=ok → OK
  - POST rescan → staff-allowed, runs scan_batch

Pattern mirrors tests/test_bp_mobile_routes.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import pytest


# ── Seed helpers ─────────────────────────────────────────────────────────────

def _seed_review_data(db_path):
    """Insert minimal import_log + txn_review_docs + txn_review_flags rows.

    No real sales_transactions are inserted — the review GET routes only read
    from txn_review_docs/txn_review_flags (scan_batch reads sales_transactions,
    but the rescan test just verifies it runs without crashing on an empty batch).
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        INSERT OR IGNORE INTO import_log
            (id, filename, rows_imported, rows_skipped, notes, imported_at)
        VALUES (9999, 'test_sales.csv', 5, 0, 'sales', '2026-06-01 10:00:00')
    """)
    conn.execute("""
        INSERT OR IGNORE INTO txn_review_docs
            (id, batch_id, doc_base, date_iso, customer, customer_code,
             line_count, flag_count, max_severity, review_status)
        VALUES (8888, 9999, 'IV9900001', '2026-06-01', 'ร้านทดสอบ', 'T001',
                3, 1, 'high', 'pending')
    """)
    conn.execute("""
        INSERT OR IGNORE INTO txn_review_flags
            (id, doc_review_id, batch_id, doc_no, rule_code, severity, message_th)
        VALUES (7777, 8888, 9999, 'IV9900001-1', 'R1_UNMAPPED', 'high',
                'ยังไม่ได้ผูกสินค้า — รหัส BSN TEST123 (ทดสอบ)')
    """)
    conn.commit()
    conn.close()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def review_client(tmp_db):
    """Admin test client with seeded review data."""
    _seed_review_data(tmp_db)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


@pytest.fixture
def staff_client(tmp_db):
    """Staff-role test client with seeded review data."""
    _seed_review_data(tmp_db)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 2
        sess['username'] = 'test-staff'
        sess['role']     = 'staff'
    return c


@pytest.fixture
def anon_client(tmp_db):
    """Unauthenticated test client with seeded data."""
    _seed_review_data(tmp_db)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app.test_client()


# ── GET route tests ──────────────────────────────────────────────────────────

def test_review_index_anon_redirects_to_login(anon_client):
    """Anonymous GET /review → redirect to /login."""
    resp = anon_client.get('/review', follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_review_index_redirects_to_batch_or_list(review_client):
    """/review redirect to either latest batch or /review/batches."""
    resp = review_client.get('/review', follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert '/review' in loc  # /review/batch/... or /review/batches


def test_review_batches_renders(review_client):
    """/review/batches returns 200."""
    resp = review_client.get('/review/batches')
    assert resp.status_code == 200, resp.data[:500]


def test_review_batch_renders_flagged_doc(review_client):
    """/review/batch/<id> renders and shows the seeded flagged doc."""
    resp = review_client.get('/review/batch/9999')
    assert resp.status_code == 200, resp.data[:500]
    assert b'IV9900001' in resp.data


def test_review_batch_not_found_returns_200(review_client):
    """/review/batch for an unknown batch still renders (empty state)."""
    resp = review_client.get('/review/batch/1')
    assert resp.status_code == 200, resp.data[:500]


# ── POST whitelist / auth tests ──────────────────────────────────────────────

def test_mark_doc_anon_redirects_to_login(anon_client):
    """Anonymous POST mark_doc → login redirect."""
    resp = anon_client.post('/review/doc/8888',
                            data={'status': 'ok'},
                            follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_rescan_anon_redirects_to_login(anon_client):
    """Anonymous POST rescan → login redirect."""
    resp = anon_client.post('/review/batch/9999/rescan',
                            follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_mark_doc_staff_is_allowed(staff_client):
    """Staff can POST mark_doc (endpoint is in _STAFF_POST_OK)."""
    resp = staff_client.post('/review/doc/8888',
                             data={'status': 'ok', 'note': '', 'batch_id': '9999'},
                             follow_redirects=True)
    assert resp.status_code == 200


def test_rescan_staff_is_allowed(staff_client):
    """Staff can POST rescan (endpoint is in _STAFF_POST_OK).

    scan_batch on batch 9999 has no sales_transactions rows so it runs
    cleanly and produces 0 docs — the rescan route flash + redirect = 200.
    """
    resp = staff_client.post('/review/batch/9999/rescan',
                             follow_redirects=True)
    assert resp.status_code == 200


# ── mark_doc validation tests ────────────────────────────────────────────────

def test_mark_doc_wrong_without_note_rejected(review_client):
    """POST status=wrong with empty note is rejected with Thai message."""
    resp = review_client.post('/review/doc/8888',
                              data={'status': 'wrong', 'note': '',
                                    'batch_id': '9999'},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert 'กรุณาระบุหมายเหตุ' in resp.data.decode('utf-8')


def test_mark_doc_wrong_with_note_accepted(review_client):
    """POST status=wrong with a non-empty note succeeds."""
    resp = review_client.post('/review/doc/8888',
                              data={'status': 'wrong', 'note': 'ราคาผิด',
                                    'batch_id': '9999'},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert 'กรุณาระบุหมายเหตุ' not in resp.data.decode('utf-8')


def test_mark_doc_ok_no_note_accepted(review_client):
    """POST status=ok without note is valid."""
    resp = review_client.post('/review/doc/8888',
                              data={'status': 'ok', 'note': '',
                                    'batch_id': '9999'},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert 'กรุณาระบุหมายเหตุ' not in resp.data.decode('utf-8')
