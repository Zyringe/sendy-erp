"""Review-hooks end-to-end tests (v2 — scan_after_import API).

Synthesizes a minimal sales fixture → scan_after_import → asserts
flags exist + suspicious_count > 0.  Also exercises the unified_import_confirm
wiring to verify the scan hook fires and the result page shows the review link.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import sys

import pytest

# ── minimal stub DB ───────────────────────────────────────────────────────────

def _make_db(tmp_path):
    db_path = str(tmp_path / "hooks_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE import_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            rows_imported INTEGER DEFAULT 0,
            rows_skipped INTEGER DEFAULT 0,
            notes TEXT,
            imported_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE sales_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            date_iso TEXT,
            doc_no TEXT,
            doc_base TEXT,
            product_id INTEGER,
            bsn_code TEXT,
            product_name_raw TEXT,
            customer TEXT,
            customer_code TEXT,
            qty REAL DEFAULT 1,
            unit TEXT DEFAULT 'ตัว',
            unit_price REAL DEFAULT 0,
            vat_type INTEGER DEFAULT 0,
            discount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            net REAL DEFAULT 0,
            ref_invoice TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            product_name TEXT,
            unit_type TEXT DEFAULT 'ตัว',
            cost_price REAL DEFAULT 0.0,
            base_sell_price REAL DEFAULT 0.0
        );
        CREATE TABLE unit_conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            bsn_unit TEXT,
            ratio REAL,
            UNIQUE(product_id, bsn_unit)
        );
        CREATE TABLE promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            promo_name TEXT,
            promo_type TEXT,
            discount_value REAL,
            start_date TEXT,
            end_date TEXT
        );
        CREATE TABLE product_price_tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            qty_label TEXT,
            price REAL
        );
        -- v2 schema: doc_base PK, no review_status, no batch_id
        CREATE TABLE txn_review_docs (
            doc_base        TEXT PRIMARY KEY,
            date_iso        TEXT NOT NULL,
            customer        TEXT,
            customer_code   TEXT,
            line_count      INTEGER NOT NULL DEFAULT 0,
            flag_count      INTEGER NOT NULL DEFAULT 0,
            max_severity    TEXT,
            free_goods_note TEXT,
            scanned_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE txn_review_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_base TEXT NOT NULL REFERENCES txn_review_docs(doc_base) ON DELETE CASCADE,
            txn_id INTEGER,
            doc_no TEXT NOT NULL,
            rule_code TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
            message_th TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _seed_sales_batch(db_path):
    """Insert a sales batch with one unmapped line (product_id NULL → R1_UNMAPPED)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO import_log (id, filename, rows_imported, notes) VALUES (1,'test.csv',1,'sales')"
    )
    conn.execute("""
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, customer, customer_code, qty, unit,
             unit_price, vat_type, discount, total, net)
        VALUES (1,'2026-06-01','IV0001-1','IV0001', NULL,'BSN-X999',
                'สินค้าทดสอบ','ร้านทดสอบ','T001',
                1,'ตัว',100,0,0,100,100)
    """)
    conn.commit()
    conn.close()
    return 1  # batch_id


# ── engine-layer tests ────────────────────────────────────────────────────────

def test_scan_after_import_flags_unmapped(tmp_path, monkeypatch):
    """scan_after_import on a batch with an unmapped line produces docs_flagged >= 1."""
    db_path = _make_db(tmp_path)
    batch_id = _seed_sales_batch(db_path)

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    result = rr.scan_after_import(batch_id, db_path=db_path)

    assert result['docs_flagged'] >= 1
    assert result['docs_scanned'] >= 1


def test_suspicious_count_after_scan(tmp_path, monkeypatch):
    """suspicious_count() > 0 after a batch with flagged docs is scanned."""
    db_path = _make_db(tmp_path)
    batch_id = _seed_sales_batch(db_path)

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    rr.scan_after_import(batch_id, db_path=db_path)
    count = rr.suspicious_count(db_path=db_path)

    assert count > 0


def test_scan_after_import_never_raises_on_empty_batch(tmp_path, monkeypatch):
    """scan_after_import on a batch with no lines returns zeros (no crash)."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO import_log (id, filename, notes) VALUES (99,'empty.csv','sales')")
    conn.commit()
    conn.close()

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    result = rr.scan_after_import(99, db_path=db_path)
    assert result['docs_scanned'] == 0
    assert result['docs_flagged'] == 0


# ── route-level hook tests ────────────────────────────────────────────────────
from io import BytesIO


def _payments_in_file():
    """Header-only cp874 report so /import-data detects + stages a file."""
    body = (
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                หน้า   :        1"\n'
        '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"\n'
        '"วันที่จาก   1 ม.ค. 2567  ถึง  31 ธ.ค. 2569"\n'
        '">>>> จบรายงาน <<<<"\n'
    ).encode('cp874')
    return BytesIO(body)


@pytest.fixture
def admin_client(tmp_db, monkeypatch):
    import models as _models
    monkeypatch.setattr(_models, 'count_pending_suggestions', lambda: 0)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _stage_and_token(client):
    client.post('/import-data',
                data={'files': (_payments_in_file(), 'การรับชำระหนี้_x.csv')},
                content_type='multipart/form-data')
    with client.session_transaction() as sess:
        return sess['import_stage']['token']


def test_sales_import_triggers_scan_and_links(admin_client, monkeypatch):
    """A committed sales import calls scan_after_import with its batch_id and
    renders the 'ไปตรวจบิล' link with the flagged count."""
    import import_router
    import review_rules
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **k: {'summary': {'batch_id': 777}})
    calls = []
    monkeypatch.setattr(review_rules, 'scan_after_import',
                        lambda bid, *a, **k: calls.append(bid) or
                        {'docs_flagged': 2, 'docs_scanned': 2})

    token = _stage_and_token(admin_client)
    resp = admin_client.post('/import-data/confirm',
                             data={'token': token, 'type_0': 'sales'})
    body = resp.data.decode('utf-8')

    assert resp.status_code == 200
    assert calls == [777], 'scan_after_import must be called once with the sales batch_id'
    assert 'ไปตรวจบิล' in body
    assert '2 ใบ' in body


def test_scan_failure_does_not_fail_the_import(admin_client, monkeypatch):
    """If scan_after_import raises, the sales import still succeeds — the error
    is caught as an inner warning, NOT a per-file failure."""
    import import_router
    import review_rules
    monkeypatch.setattr(import_router, 'commit_file',
                        lambda *a, **k: {'summary': {'batch_id': 777}})

    def _boom(*a, **k):
        raise RuntimeError('BOOM_SCAN_FAIL')
    monkeypatch.setattr(review_rules, 'scan_after_import', _boom)

    token = _stage_and_token(admin_client)
    resp = admin_client.post('/import-data/confirm',
                             data={'token': token, 'type_0': 'sales'})
    body = resp.data.decode('utf-8')

    assert resp.status_code == 200
    assert 'ผลการนำเข้า' in body
    assert 'สแกนตรวจบิลไม่สำเร็จ: BOOM_SCAN_FAIL' in body
