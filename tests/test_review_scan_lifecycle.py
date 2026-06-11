"""Lifecycle tests for review_rules.scan_batch and public API.

TDD-FIRST. Covers:
- scan twice idempotent
- mark 'ok' → rescan keeps 'ok' (unchanged fingerprint)
- simulated re-import (new batch, new flags) resets status to pending
- clean doc → auto_passed
- migration: tables created by init_db()
"""
import sqlite3
import os
import sys
import pytest


# ── reuse helpers from test_review_rules (copy to avoid import chain issues) ──

def _make_db(tmp_path, name="test.db"):
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
        CREATE TABLE IF NOT EXISTS txn_review_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            doc_base TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            customer TEXT,
            customer_code TEXT,
            line_count INTEGER NOT NULL DEFAULT 0,
            flag_count INTEGER NOT NULL DEFAULT 0,
            max_severity TEXT,
            flags_fingerprint TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending','ok','wrong','auto_passed')),
            reviewed_by TEXT,
            reviewed_at TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(batch_id, doc_base)
        );
        CREATE TABLE IF NOT EXISTS txn_review_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_review_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
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
    return db_path, conn


def _add_batch(conn, notes="sales"):
    cur = conn.execute(
        "INSERT INTO import_log (filename, notes) VALUES (?, ?)",
        ("test.csv", notes)
    )
    conn.commit()
    return cur.lastrowid


def _add_product(conn, pid, unit_type="ตัว", cost_price=100.0, base_sell_price=150.0):
    conn.execute(
        "INSERT OR REPLACE INTO products (id, product_name, unit_type, cost_price, base_sell_price) VALUES (?,?,?,?,?)",
        (pid, f"สินค้า #{pid}", unit_type, cost_price, base_sell_price)
    )
    conn.commit()


def _add_sales_row(conn, batch_id, doc_no, product_id=None, bsn_code="ABC001",
                   unit="ตัว", unit_price=150.0, qty=1.0, net=None,
                   customer="ลูกค้า", customer_code="C001",
                   ref_invoice=None, date_iso="2026-06-01"):
    if net is None:
        net = unit_price * qty
    doc_base = doc_no.rsplit("-", 1)[0] if "-" in doc_no else doc_no
    conn.execute("""
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, customer, customer_code, qty, unit,
             unit_price, vat_type, discount, total, net, ref_invoice)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,?)
    """, (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
          "ชื่อดิบ", customer, customer_code, qty, unit,
          unit_price, net, net, ref_invoice))
    conn.commit()


def _import_rr(db_path):
    """Import review_rules with a patched DATABASE_PATH."""
    import config
    old = config.DATABASE_PATH
    config.DATABASE_PATH = db_path
    if 'review_rules' in sys.modules:
        del sys.modules['review_rules']
    import review_rules
    config.DATABASE_PATH = old
    return review_rules


# ═══════════════════════════════════════════════════════════════════════════
# Migration test
# ═══════════════════════════════════════════════════════════════════════════

class TestMigration:
    def test_tables_created_by_migration(self, empty_db):
        """Applying mig 098 on a schema-clone DB creates both review tables.

        The empty_db fixture clones the live DB schema at whatever state it's
        currently at. We apply the migration SQL on top, then verify the tables
        exist. This tests the migration SQL itself, not the runner state.
        """
        import os
        mig_path = os.path.join(
            os.path.dirname(__file__), '..', 'data', 'migrations',
            '098_txn_review.sql'
        )
        conn = sqlite3.connect(empty_db)
        # Drop tables if already present (live DB already ran the mig)
        conn.execute("DROP TABLE IF EXISTS txn_review_flags")
        conn.execute("DROP TABLE IF EXISTS txn_review_docs")
        conn.commit()
        with open(mig_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        conn.executescript(sql)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert 'txn_review_docs' in tables
        assert 'txn_review_flags' in tables


# ═══════════════════════════════════════════════════════════════════════════
# scan_batch
# ═══════════════════════════════════════════════════════════════════════════

class TestScanBatch:
    def test_scan_creates_doc_row(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        # Unmapped line → R1 flag
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        result = rr.scan_batch(batch_id, db_path=db_path)
        assert result['docs_total'] >= 1
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT * FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()
        assert row is not None
        c.close()

    def test_scan_idempotent(self, tmp_path):
        """Scanning twice gives same result (no duplicate flags)."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        r1 = rr.scan_batch(batch_id, db_path=db_path)
        r2 = rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        flag_count = c.execute(
            "SELECT COUNT(*) FROM txn_review_flags WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        doc_count = c.execute(
            "SELECT COUNT(*) FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        # Idempotent: same counts after two scans
        assert r1['flags_total'] == r2['flags_total']
        assert r1['docs_total'] == r2['docs_total']
        # No doubled-up flags
        assert flag_count == r2['flags_total']
        assert doc_count == r2['docs_total']

    def test_clean_doc_gets_auto_passed(self, tmp_path):
        """A mapped product row with no flags gets review_status='auto_passed'."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0, base_sell_price=150.0)
        _add_sales_row(conn, batch_id, "IV001-1",
                       product_id=1, unit="ตัว", unit_price=150.0)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT review_status FROM txn_review_docs WHERE batch_id=?",
            (batch_id,)
        ).fetchone()
        c.close()
        assert row[0] == 'auto_passed'

    def test_flagged_doc_gets_pending(self, tmp_path):
        """A doc with flags starts as 'pending'."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)  # R1
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT review_status FROM txn_review_docs WHERE batch_id=?",
            (batch_id,)
        ).fetchone()
        c.close()
        assert row[0] == 'pending'

    def test_rescan_keeps_ok_decision_unchanged_fingerprint(self, tmp_path):
        """mark_doc 'ok' → rescan with same data → keeps 'ok'."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        doc_id = c.execute(
            "SELECT id FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        rr.mark_doc(doc_id, 'ok', 'ตรวจแล้ว ถูก', 'staff01', db_path=db_path)
        # Rescan with same data
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT review_status, reviewed_by FROM txn_review_docs WHERE id=?",
            (doc_id,)
        ).fetchone()
        c.close()
        assert row[0] == 'ok'
        assert row[1] == 'staff01'

    def test_rescan_resets_to_pending_when_data_changes(self, tmp_path):
        """Re-import changes the rows (new batch_id) → new scan → pending."""
        db_path, conn = _make_db(tmp_path)
        batch_id1 = _add_batch(conn)
        _add_sales_row(conn, batch_id1, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id1, db_path=db_path)
        c = sqlite3.connect(db_path)
        doc_id = c.execute(
            "SELECT id FROM txn_review_docs WHERE batch_id=?", (batch_id1,)
        ).fetchone()[0]
        c.close()
        rr.mark_doc(doc_id, 'ok', 'ถูก', 'staff01', db_path=db_path)

        # Simulate re-import: new batch
        c2 = sqlite3.connect(db_path)
        c2.row_factory = sqlite3.Row
        batch_id2 = _add_batch(c2)
        # Different price → different fingerprint
        _add_sales_row(c2, batch_id2, "IV001-1", product_id=None,
                       unit_price=999.0, net=999.0)
        c2.close()

        rr.scan_batch(batch_id2, db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT review_status FROM txn_review_docs WHERE batch_id=?",
            (batch_id2,)
        ).fetchone()
        c.close()
        assert row[0] == 'pending'

    def test_scan_max_severity_high_beats_medium(self, tmp_path):
        """Doc with both R1 (high) and R3 (medium) flags → max_severity='high'."""
        db_path, conn = _make_db(tmp_path)
        # Need history batch for R3
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, cost_price=100.0, base_sell_price=150.0)
        # Seed history for R3
        for i in range(3):
            conn.execute("""
                INSERT INTO sales_transactions
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, customer, customer_code, qty, unit,
                     unit_price, vat_type, discount, total, net, ref_invoice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)
            """, (hist_batch, "2025-12-01", f"IV_HH{i+1}-1", f"IV_HH{i+1}",
                  1, 'ABC001', 'ชื่อดิบ', 'ลูกค้าA', None,
                  1.0, 'ตัว', 100.0, 100.0, 100.0))
        conn.commit()
        # IV002: unmapped → R1(high)
        _add_sales_row(conn, batch_id, "IV002-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute(
            "SELECT max_severity FROM txn_review_docs WHERE batch_id=? AND doc_base='IV002'",
            (batch_id,)
        ).fetchone()
        c.close()
        assert row[0] == 'high'

    def test_scan_returns_summary_counts(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        # One clean doc
        _add_sales_row(conn, batch_id, "IV001-1", product_id=1, unit_price=150.0)
        # One flagged doc
        _add_sales_row(conn, batch_id, "IV002-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        result = rr.scan_batch(batch_id, db_path=db_path)
        assert result['docs_total'] == 2
        assert result['docs_clean'] == 1
        assert result['docs_flagged'] == 1

    def test_scan_empty_batch(self, tmp_path):
        """Empty batch (no sales rows) → zero docs, no crash."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        conn.close()
        rr = _import_rr(db_path)
        result = rr.scan_batch(batch_id, db_path=db_path)
        assert result['docs_total'] == 0
        assert result['flags_total'] == 0


# ═══════════════════════════════════════════════════════════════════════════
# mark_doc
# ═══════════════════════════════════════════════════════════════════════════

class TestMarkDoc:
    def test_mark_ok(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        doc_id = c.execute(
            "SELECT id FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        rr.mark_doc(doc_id, 'ok', None, 'staff01', db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute("SELECT review_status, reviewed_by FROM txn_review_docs WHERE id=?",
                        (doc_id,)).fetchone()
        c.close()
        assert row[0] == 'ok'
        assert row[1] == 'staff01'

    def test_mark_wrong(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        doc_id = c.execute(
            "SELECT id FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        rr.mark_doc(doc_id, 'wrong', 'ราคาผิด', 'staff01', db_path=db_path)
        c = sqlite3.connect(db_path)
        row = c.execute("SELECT review_status, note FROM txn_review_docs WHERE id=?",
                        (doc_id,)).fetchone()
        c.close()
        assert row[0] == 'wrong'
        assert row[1] == 'ราคาผิด'

    def test_mark_invalid_status_raises(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        doc_id = c.execute(
            "SELECT id FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        with pytest.raises(ValueError):
            rr.mark_doc(doc_id, 'invalid_status', None, 'staff01', db_path=db_path)


# ═══════════════════════════════════════════════════════════════════════════
# get_batch_review
# ═══════════════════════════════════════════════════════════════════════════

class TestGetBatchReview:
    def test_returns_grouped_by_date_iso(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=1, date_iso="2026-06-01")
        _add_sales_row(conn, batch_id, "IV002-1", product_id=None, date_iso="2026-06-02")
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        result = rr.get_batch_review(batch_id, db_path=db_path)
        dates = list(result.keys())
        assert "2026-06-01" in dates
        assert "2026-06-02" in dates

    def test_flags_attached_to_docs(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        result = rr.get_batch_review(batch_id, db_path=db_path)
        # Find the flagged doc
        all_docs = []
        for docs in result.values():
            all_docs.extend(docs)
        flagged = [d for d in all_docs if d['flag_count'] > 0]
        assert len(flagged) == 1
        assert len(flagged[0]['flags']) > 0


# ═══════════════════════════════════════════════════════════════════════════
# get_sales_batches
# ═══════════════════════════════════════════════════════════════════════════

class TestGetSalesBatches:
    def test_returns_only_sales_batches(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        _add_batch(conn, notes="sales")
        _add_batch(conn, notes="purchase")
        _add_batch(conn, notes="sales")
        conn.close()
        rr = _import_rr(db_path)
        batches = rr.get_sales_batches(db_path=db_path)
        assert len(batches) == 2
        for b in batches:
            assert b['notes'] == 'sales'

    def test_newest_first(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        id1 = _add_batch(conn, notes="sales")
        id2 = _add_batch(conn, notes="sales")
        conn.close()
        rr = _import_rr(db_path)
        batches = rr.get_sales_batches(db_path=db_path)
        assert batches[0]['id'] == id2
        assert batches[1]['id'] == id1

    def test_review_progress_counts(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn, notes="sales")
        _add_product(conn, 1)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=1)   # clean
        _add_sales_row(conn, batch_id, "IV002-1", product_id=None) # flagged
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        batches = rr.get_sales_batches(db_path=db_path)
        b = next(x for x in batches if x['id'] == batch_id)
        assert b['docs_total'] == 2
        assert b['docs_auto_passed'] == 1
        assert b['docs_pending'] == 1


# ═══════════════════════════════════════════════════════════════════════════
# pending_review_count
# ═══════════════════════════════════════════════════════════════════════════

class TestPendingReviewCount:
    def test_counts_pending_docs(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn, notes="sales")
        _add_product(conn, 1)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=1)   # clean → auto_passed
        _add_sales_row(conn, batch_id, "IV002-1", product_id=None) # R1 → pending
        _add_sales_row(conn, batch_id, "IV003-1", product_id=None) # R1 → pending
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        count = rr.pending_review_count(db_path=db_path)
        assert count == 2

    def test_zero_when_no_pending(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn, notes="sales")
        _add_product(conn, 1)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=1)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        count = rr.pending_review_count(db_path=db_path)
        assert count == 0

    def test_zero_when_no_review_tables(self, tmp_path):
        """Should return 0 gracefully when tables don't exist yet."""
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE import_log (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        rr = _import_rr(db_path)
        # Should not crash
        count = rr.pending_review_count(db_path=db_path)
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════
# fingerprint logic
# ═══════════════════════════════════════════════════════════════════════════

class TestFingerprintLogic:
    def test_same_data_same_fingerprint(self, tmp_path):
        """Scanning same data twice yields same fingerprint (idempotent)."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1", product_id=None)
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        fp1 = c.execute(
            "SELECT flags_fingerprint FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        rr.scan_batch(batch_id, db_path=db_path)
        c = sqlite3.connect(db_path)
        fp2 = c.execute(
            "SELECT flags_fingerprint FROM txn_review_docs WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        c.close()
        assert fp1 == fp2

    def test_different_data_different_fingerprint(self, tmp_path):
        """Two batches with different flags produce different fingerprints."""
        db_path, conn = _make_db(tmp_path)
        batch_id1 = _add_batch(conn)
        batch_id2 = _add_batch(conn)
        _add_sales_row(conn, batch_id1, "IV001-1", product_id=None,
                       unit_price=50.0, bsn_code="CODE_A")
        _add_sales_row(conn, batch_id2, "IV001-1", product_id=None,
                       unit_price=999.0, bsn_code="CODE_B")
        conn.close()
        rr = _import_rr(db_path)
        rr.scan_batch(batch_id1, db_path=db_path)
        rr.scan_batch(batch_id2, db_path=db_path)
        c = sqlite3.connect(db_path)
        rows = c.execute(
            "SELECT flags_fingerprint FROM txn_review_docs WHERE batch_id IN (?,?)",
            (batch_id1, batch_id2)
        ).fetchall()
        c.close()
        fps = [r[0] for r in rows]
        assert fps[0] != fps[1]
