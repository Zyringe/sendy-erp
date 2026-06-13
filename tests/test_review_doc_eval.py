"""Tests for _evaluate_doc, _persist_doc, scan_all/scan_docs, get_review_feed.

TDD-first. Covers Tasks 4-6. Uses v2 schema (doc_base PK, free_goods_note).
"""
import sqlite3
import sys
import os
import importlib
import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_db(tmp_path, name="test.db"):
    """Return db_path with v2 schema (txn_review_docs has doc_base PK, free_goods_note)."""
    db_path = str(tmp_path / name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS import_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            rows_imported INTEGER DEFAULT 0,
            rows_skipped INTEGER DEFAULT 0,
            notes TEXT,
            imported_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            product_name TEXT,
            unit_type TEXT DEFAULT 'ตัว',
            cost_price REAL DEFAULT 0.0,
            base_sell_price REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS unit_conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            bsn_unit TEXT,
            ratio REAL,
            UNIQUE(product_id, bsn_unit)
        );

        CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            promo_name TEXT,
            promo_type TEXT,
            discount_value REAL,
            date_start TEXT,
            date_end TEXT,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS product_price_tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            qty_label TEXT,
            price REAL
        );

        CREATE TABLE IF NOT EXISTS sales_transactions (
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
            qty REAL DEFAULT 1.0,
            unit TEXT,
            unit_price REAL,
            vat_type INTEGER DEFAULT 1,
            discount TEXT,
            total REAL,
            net REAL,
            ref_invoice TEXT
        );

        -- v2 schema: doc_base PK, free_goods_note, no review_status
        CREATE TABLE IF NOT EXISTS txn_review_docs (
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

        CREATE TABLE IF NOT EXISTS txn_review_flags (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_base      TEXT NOT NULL REFERENCES txn_review_docs(doc_base) ON DELETE CASCADE,
            txn_id        INTEGER,
            doc_no        TEXT NOT NULL,
            rule_code     TEXT NOT NULL,
            severity      TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
            message_th    TEXT NOT NULL,
            details_json  TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.close()
    return db_path


def _import_rr(db_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'inventory_app'))
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    os.environ.setdefault('SECRET_KEY', 'test')
    os.environ.setdefault('ADMIN_PASSWORD', 'test')
    import review_rules as rr_mod
    importlib.reload(rr_mod)
    rr_mod.DB_PATH = db_path
    return rr_mod


def _add_batch(conn):
    conn.execute("INSERT INTO import_log(filename) VALUES ('test.xls')")
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_product(conn, pid, unit_type='ตัว', cost_price=0.0, base_sell_price=0.0):
    conn.execute("""
        INSERT OR REPLACE INTO products(id, product_name, unit_type, cost_price, base_sell_price)
        VALUES (?,?,?,?,?)
    """, (pid, f'Product {pid}', unit_type, cost_price, base_sell_price))
    conn.commit()


def _add_sales_row(conn, batch_id, doc_base, product_id=1, qty=1.0, unit='ตัว',
                   unit_price=100.0, net=100.0, ref_invoice=None,
                   customer='ลูกค้าA', customer_code=None, date_iso='2026-06-01'):
    doc_no = doc_base
    conn.execute("""
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, customer, customer_code, qty, unit,
             unit_price, vat_type, discount, total, net, ref_invoice)
        VALUES (?,?,?,?,?,'CODE','ชื่อดิบ',?,?,?,?,?,1,NULL,?,?,?)
    """, (batch_id, date_iso, doc_no, doc_base, product_id,
          customer, customer_code, qty, unit, unit_price, net, net, ref_invoice))
    conn.commit()


# ── Task 4: _evaluate_doc ─────────────────────────────────────────────────────

class TestEvaluateDoc:
    def test_paidplusfree_doc_good_prices_clean(self, tmp_path):
        """Paid line priced fine + free line → real_flag_count=0; free_goods_note present."""
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0, base_sell_price=150.0)
        _add_sales_row(conn, bid, "IV100", product_id=1, qty=100, unit="ตัว",
                       unit_price=150, net=15000)
        _add_sales_row(conn, bid, "IV100", product_id=1, qty=25,  unit="ตัว",
                       unit_price=0, net=0)
        conn.commit()
        rr = _import_rr(db_path)
        lines = [dict(r) for r in conn.execute(
            "SELECT * FROM sales_transactions WHERE doc_base='IV100'"
        )]
        ev = rr._evaluate_doc(conn, lines)
        assert ev['real_flag_count'] == 0
        assert ev['free_goods_note'] is not None
        assert 'แถม' in ev['free_goods_note']
        assert '25' in ev['free_goods_note']

    def test_all_free_doc_surfaces_r7(self, tmp_path):
        """All lines are free → R7_ALL_FREE flag (low severity)."""
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0)
        _add_sales_row(conn, bid, "IV200", product_id=1, qty=10, unit="ตัว",
                       unit_price=0, net=0)
        conn.commit()
        rr = _import_rr(db_path)
        lines = [dict(r) for r in conn.execute(
            "SELECT * FROM sales_transactions WHERE doc_base='IV200'"
        )]
        ev = rr._evaluate_doc(conn, lines)
        codes = {fl['rule_code'] for _, fl in ev['flags']}
        assert 'R7_ALL_FREE' in codes
        assert ev['max_severity'] == 'low'

    def test_flagged_doc_has_correct_max_severity(self, tmp_path):
        """High-severity flag dominates."""
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_product(conn, 1, cost_price=200.0, base_sell_price=300.0)
        # sell below cost → R2 (high)
        _add_sales_row(conn, bid, "IV300", product_id=1, qty=1, unit="ตัว",
                       unit_price=50, net=50)
        conn.commit()
        rr = _import_rr(db_path)
        lines = [dict(r) for r in conn.execute(
            "SELECT * FROM sales_transactions WHERE doc_base='IV300'"
        )]
        ev = rr._evaluate_doc(conn, lines)
        assert ev['max_severity'] == 'high'
        codes = {fl['rule_code'] for _, fl in ev['flags']}
        assert 'R2_BELOW_COST' in codes


# ── Task 5: scan_all / scan_docs / scan_after_import ─────────────────────────

class TestScanAll:
    def test_scan_all_persists_only_suspicious(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0, base_sell_price=150.0)
        _add_sales_row(conn, bid, "IVCLEAN", product_id=1, qty=1, unit="ตัว",
                       unit_price=150, net=150)
        _add_sales_row(conn, bid, "IVBAD",   product_id=None, qty=1, unit="ตัว",
                       unit_price=10, net=10)
        conn.commit()
        res = rr.scan_all(db_path=db_path)
        rows = [r[0] for r in sqlite3.connect(db_path).execute(
            "SELECT doc_base FROM txn_review_docs"
        )]
        assert "IVBAD" in rows
        assert "IVCLEAN" not in rows
        assert res['docs_flagged'] == 1

    def test_scan_all_idempotent(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVX", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5)
        conn.commit()
        r1 = rr.scan_all(db_path=db_path)
        r2 = rr.scan_all(db_path=db_path)
        assert r1 == r2
        n = sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM txn_review_docs"
        ).fetchone()[0]
        assert n == 1

    def test_scan_docs_removes_row_when_doc_becomes_clean(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVY", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5)
        conn.commit()
        rr.scan_all(db_path=db_path)
        conn.execute("UPDATE sales_transactions SET product_id=1 WHERE doc_base='IVY'")
        _add_product(conn, 1, cost_price=1.0, base_sell_price=4.0)
        conn.commit()
        rr.scan_docs(["IVY"], db_path=db_path)
        n = sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM txn_review_docs WHERE doc_base='IVY'"
        ).fetchone()[0]
        assert n == 0

    def test_scan_after_import_rescans_docs_in_batch(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVZ", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5)
        conn.commit()
        res = rr.scan_after_import(bid, db_path=db_path)
        assert res['docs_scanned'] == 1
        n = sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM txn_review_docs WHERE doc_base='IVZ'"
        ).fetchone()[0]
        assert n == 1


# ── Task 6: get_review_feed + suspicious_count + default_since ───────────────

class TestReviewFeed:
    def test_feed_newest_first_with_flags(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVA", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5, date_iso="2026-01-01")
        _add_sales_row(conn, bid, "IVB", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5, date_iso="2026-06-01")
        conn.commit()
        rr.scan_all(db_path=db_path)
        feed = rr.get_review_feed(db_path=db_path)
        assert [d['doc_base'] for d in feed] == ["IVB", "IVA"]
        assert feed[0]['flags'][0]['rule_code'] == 'R1_UNMAPPED'

    def test_feed_since_filter_and_count(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVOLD", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5, date_iso="2024-01-01")
        _add_sales_row(conn, bid, "IVNEW", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5, date_iso="2026-06-01")
        conn.commit()
        rr.scan_all(db_path=db_path)
        assert [d['doc_base'] for d in rr.get_review_feed(
            since_date="2026-01-01", db_path=db_path
        )] == ["IVNEW"]
        assert rr.suspicious_count(since_date="2026-01-01", db_path=db_path) == 1
        assert rr.suspicious_count(db_path=db_path) == 2

    def test_default_since_is_roughly_183_days_back(self, tmp_path):
        from datetime import date, timedelta
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        ds = rr.default_since()
        expected = (date.today() - timedelta(days=183)).isoformat()
        assert ds == expected


# ── Option A: severity filter — hide 'medium'-only (R3/R5) docs by default ────

class TestSeverityFilter:
    """The feed and badge hide docs whose worst issue is only 'medium' (R3/R5),
    which fire heavily on normal negotiated B2B prices. include_medium=True shows
    them. High (R1/R2/R4) and low (R7) docs are always shown."""

    def _seed_one_medium_doc(self, db_path):
        """One R3 price-deviation doc (medium) + 3 clean history docs."""
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0, base_sell_price=150.0)
        # 3 history docs @100 (global R3 baseline), earlier date so they
        # themselves lack enough prior history to flag
        for db in ("IVH1", "IVH2", "IVH3"):
            _add_sales_row(conn, bid, db, product_id=1, qty=1, unit="ตัว",
                           unit_price=100, net=100, date_iso="2026-05-01")
        # target deviates +100% vs median 100 → R3 medium
        _add_sales_row(conn, bid, "IVDEV", product_id=1, qty=1, unit="ตัว",
                       unit_price=200, net=200, date_iso="2026-06-01")
        conn.commit()
        rr.scan_all(db_path=db_path)
        return rr

    def test_medium_doc_hidden_by_default_shown_with_toggle(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = self._seed_one_medium_doc(db_path)
        shown = rr.get_review_feed(include_medium=True, db_path=db_path)
        assert "IVDEV" in [d['doc_base'] for d in shown]
        assert all(d['max_severity'] == 'medium' for d in shown)
        hidden = rr.get_review_feed(db_path=db_path)
        assert "IVDEV" not in [d['doc_base'] for d in hidden]

    def test_suspicious_count_respects_include_medium(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = self._seed_one_medium_doc(db_path)
        assert rr.suspicious_count(db_path=db_path) == 0
        assert rr.suspicious_count(include_medium=True, db_path=db_path) == 1

    def test_high_doc_always_shown(self, tmp_path):
        db_path = _make_db(tmp_path)
        rr = _import_rr(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bid = _add_batch(conn)
        _add_sales_row(conn, bid, "IVHI", product_id=None, qty=1, unit="ตัว",
                       unit_price=5, net=5, date_iso="2026-06-01")  # R1 high
        conn.commit()
        rr.scan_all(db_path=db_path)
        assert "IVHI" in [d['doc_base'] for d in rr.get_review_feed(db_path=db_path)]
        assert rr.suspicious_count(db_path=db_path) == 1
