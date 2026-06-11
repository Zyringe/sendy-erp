"""P5 review-hooks end-to-end test.

Synthesizes a minimal sales fixture (not real data) → scan_batch → asserts
flags exist + pending_review_count > 0.  No Flask client needed here — the
hook is tested at the module layer; import_box/dashboard are template-level
and covered by the existing bp_review route tests.
"""
import sqlite3
import sys
import os

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
        CREATE TABLE txn_review_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            doc_base TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            customer TEXT, customer_code TEXT,
            line_count INTEGER NOT NULL DEFAULT 0,
            flag_count INTEGER NOT NULL DEFAULT 0,
            max_severity TEXT,
            flags_fingerprint TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending','ok','wrong','auto_passed')),
            reviewed_by TEXT, reviewed_at TEXT, note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(batch_id, doc_base)
        );
        CREATE TABLE txn_review_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_review_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            txn_id INTEGER,
            doc_no TEXT NOT NULL,
            rule_code TEXT NOT NULL,
            severity TEXT NOT NULL,
            message_th TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX idx_txn_review_docs_batch ON txn_review_docs(batch_id, review_status);
        CREATE INDEX idx_txn_review_flags_doc ON txn_review_flags(doc_review_id);
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


# ── tests ─────────────────────────────────────────────────────────────────────

def test_scan_batch_flags_unmapped(tmp_path, monkeypatch):
    """scan_batch on a batch with an unmapped line produces docs_flagged >= 1."""
    db_path = _make_db(tmp_path)
    batch_id = _seed_sales_batch(db_path)

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    result = rr.scan_batch(batch_id, db_path=db_path)

    assert result['docs_flagged'] >= 1
    assert result['docs_total'] >= 1


def test_pending_review_count_after_scan(tmp_path, monkeypatch):
    """pending_review_count() > 0 after a batch with flagged docs is scanned."""
    db_path = _make_db(tmp_path)
    batch_id = _seed_sales_batch(db_path)

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    rr.scan_batch(batch_id, db_path=db_path)
    count = rr.pending_review_count(db_path=db_path)

    assert count > 0


def test_scan_batch_never_raises_on_empty_batch(tmp_path, monkeypatch):
    """scan_batch on a batch with no lines returns zeros (no crash)."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO import_log (id, filename, notes) VALUES (99,'empty.csv','sales')")
    conn.commit()
    conn.close()

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', db_path)
    import review_rules as rr

    result = rr.scan_batch(99, db_path=db_path)
    assert result['docs_total'] == 0
    assert result['docs_flagged'] == 0
