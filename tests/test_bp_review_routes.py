"""Route-level integration tests for bp_review v2 (ตรวจบิล read-only feed).

Covers:
  - Anonymous GET /review → login redirect
  - Anonymous POST /review/scan → login redirect
  - GET /review → 200, shows seeded flagged doc_base
  - GET /review?all=1 → 200
  - Staff POST /review/scan → allowed (whitelist); redirects to /review
  - Admin POST /review/scan → allowed; redirects to /review

Pattern mirrors tests/test_bp_mobile_routes.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import pytest


# ── Seed helpers ─────────────────────────────────────────────────────────────

def _seed_review_data(db_path):
    """Upgrade review tables to v2 schema (mig 099) and insert one flagged doc.

    The live DB the test fixtures copy has the v1 schema (098), so we apply
    migration 099 first to get the v2 doc_base-PK tables before seeding.
    """
    mig_path = os.path.join(
        os.path.dirname(__file__), '..', 'data', 'migrations',
        '099_txn_review_v2.sql',
    )
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS txn_review_flags")
    conn.execute("DROP TABLE IF EXISTS txn_review_docs")
    conn.commit()
    with open(mig_path, 'r', encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.execute("PRAGMA foreign_keys = OFF")  # executescript re-enables FK; turn off again
    conn.execute("""
        INSERT OR IGNORE INTO txn_review_docs
            (doc_base, date_iso, customer, customer_code,
             line_count, flag_count, max_severity)
        VALUES ('IV9900001', '2026-06-01', 'ร้านทดสอบ', 'T001', 3, 1, 'high')
    """)
    conn.execute("""
        INSERT OR IGNORE INTO txn_review_flags
            (doc_base, doc_no, rule_code, severity, message_th)
        VALUES ('IV9900001', 'IV9900001-1', 'R1_UNMAPPED', 'high',
                'ยังไม่ได้ผูกสินค้า — รหัส BSN TEST123')
    """)
    conn.commit()
    conn.close()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_client(tmp_db, monkeypatch, role=None):
    """Create a Flask test client with seeded review data and optional session role.

    Monkeypatches models.count_pending_suggestions to return 0 so the context
    processor doesn't trip on a table that may not exist in the schema-cloned
    test DB (it's unrelated to what these route tests exercise).
    """
    _seed_review_data(tmp_db)
    import models as _models
    monkeypatch.setattr(_models, 'count_pending_suggestions', lambda: 0)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    if role:
        with c.session_transaction() as sess:
            sess['user_id']  = 1 if role == 'admin' else 2
            sess['username'] = f'test-{role}'
            sess['role']     = role
    return c


@pytest.fixture
def review_client(tmp_db, monkeypatch):
    """Admin test client with seeded review data."""
    return _make_client(tmp_db, monkeypatch, role='admin')


@pytest.fixture
def staff_client(tmp_db, monkeypatch):
    """Staff-role test client with seeded review data."""
    return _make_client(tmp_db, monkeypatch, role='staff')


@pytest.fixture
def anon_client(tmp_db, monkeypatch):
    """Unauthenticated test client."""
    return _make_client(tmp_db, monkeypatch, role=None)


# ── GET route tests ──────────────────────────────────────────────────────────

def test_review_index_anon_redirects_to_login(anon_client):
    """Anonymous GET /review → redirect to /login."""
    resp = anon_client.get('/review', follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_review_index_renders_feed(review_client):
    """GET /review → 200, shows the seeded flagged doc."""
    resp = review_client.get('/review')
    assert resp.status_code == 200, resp.data[:500]
    assert b'IV9900001' in resp.data


def test_review_index_all_renders(review_client):
    """GET /review?all=1 → 200."""
    resp = review_client.get('/review?all=1')
    assert resp.status_code == 200, resp.data[:500]


# ── POST scan tests ──────────────────────────────────────────────────────────

def test_review_scan_anon_redirects_to_login(anon_client):
    """Anonymous POST /review/scan → login redirect."""
    resp = anon_client.post('/review/scan', follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_review_scan_staff_is_allowed(staff_client):
    """Staff can POST /review/scan (endpoint is in _STAFF_POST_OK)."""
    resp = staff_client.post('/review/scan', follow_redirects=True)
    assert resp.status_code == 200


def test_review_scan_redirects_to_index(review_client):
    """POST /review/scan → 302 to /review."""
    resp = review_client.post('/review/scan', follow_redirects=False)
    assert resp.status_code == 302
    assert '/review' in resp.headers['Location']
