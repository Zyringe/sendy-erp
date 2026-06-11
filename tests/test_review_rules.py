"""Tests for review_rules.py — detection engine (R1–R5).

TDD-FIRST: these tests are written before review_rules.py exists.
Run:
  cd ~/Sendai-Boonsawat-wt/review-engine
  ~/.virtualenvs/erp/bin/pytest tests/test_review_rules.py -x -ra

All cases listed in plan.md Phase P3 are covered here.
"""
import sqlite3
import pytest


# ── helpers to build minimal fixture DBs ────────────────────────────────────

def _make_db(tmp_path, name="test.db"):
    db_path = str(tmp_path / name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")   # simpler fixtures without full FK chain
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
        (pid, f"สินค้าทดสอบ #{pid}", unit_type, cost_price, base_sell_price)
    )
    conn.commit()


def _add_unit_conversion(conn, product_id, bsn_unit, ratio):
    conn.execute(
        "INSERT OR REPLACE INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (product_id, bsn_unit, ratio)
    )
    conn.commit()


def _add_sales_row(conn, batch_id, doc_no, product_id=None, bsn_code="ABC001",
                   unit="ตัว", unit_price=150.0, qty=1.0, net=None,
                   customer="ลูกค้าทดสอบ", customer_code="C001",
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
          "ชื่อสินค้าดิบ", customer, customer_code, qty, unit,
          unit_price, net, net, ref_invoice))
    conn.commit()


def _add_promo(conn, product_id, promo_type, discount_value,
               date_start=None, date_end=None, promo_name="โปรทดสอบ"):
    conn.execute("""
        INSERT INTO promotions (product_id, promo_name, promo_type, discount_value,
                                date_start, date_end, is_active)
        VALUES (?,?,?,?,?,?,1)
    """, (product_id, promo_name, promo_type, discount_value, date_start, date_end))
    conn.commit()


def _add_price_tier(conn, product_id, qty_label, price):
    conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
        (product_id, qty_label, price)
    )
    conn.commit()


# ── import the module under test ─────────────────────────────────────────────

def _import_rr(db_path):
    """Import review_rules with a patched DATABASE_PATH."""
    import importlib
    import sys
    import config
    # patch config before importing
    old = config.DATABASE_PATH
    config.DATABASE_PATH = db_path
    # force reload so the patched path is used by any module-level read
    if 'review_rules' in sys.modules:
        del sys.modules['review_rules']
    import review_rules
    # restore
    config.DATABASE_PATH = old
    return review_rules


# ═══════════════════════════════════════════════════════════════════════════
# R1_UNMAPPED — product_id IS NULL
# ═══════════════════════════════════════════════════════════════════════════

class TestR1Unmapped:
    def test_fires_when_product_id_null(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_sales_row(conn, batch_id, "IV001-1",
                       product_id=None, bsn_code="UNKNOWN01",
                       unit_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': None, 'bsn_code': 'UNKNOWN01',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        codes = [f['rule_code'] for f in flags]
        assert 'R1_UNMAPPED' in codes

    def test_no_fire_when_product_id_set(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 150.0,
            'qty': 1.0, 'net': 150.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R1_UNMAPPED' not in [f['rule_code'] for f in flags]

    def test_message_contains_bsn_code(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': None, 'bsn_code': 'SPECIAL99',
            'product_name_raw': 'ชื่อพิเศษ', 'unit': 'ตัว', 'unit_price': 50.0,
            'qty': 1.0, 'net': 50.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        r1 = next(f for f in flags if f['rule_code'] == 'R1_UNMAPPED')
        assert 'SPECIAL99' in r1['message_th']
        assert r1['severity'] == 'high'


# ═══════════════════════════════════════════════════════════════════════════
# R2_BELOW_COST
# ═══════════════════════════════════════════════════════════════════════════

class TestR2BelowCost:
    def test_fires_below_cost(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 50.0,
            'qty': 2.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' in [f['rule_code'] for f in flags]

    def test_no_fire_above_cost(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 110.0,
            'qty': 1.0, 'net': 110.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' not in [f['rule_code'] for f in flags]

    def test_skip_when_cost_zero(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=0.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 1.0,
            'qty': 1.0, 'net': 1.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' not in [f['rule_code'] for f in flags]

    def test_skip_sr_row(self, tmp_path):
        """SR rows (ref_invoice non-empty) skip R2."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'SR001-1', 'doc_base': 'SR001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 50.0,
            'qty': 1.0, 'net': 50.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': 'IV001',      # non-empty = SR
        })
        assert 'R2_BELOW_COST' not in [f['rule_code'] for f in flags]

    def test_skip_negative_qty_row(self, tmp_path):
        """Rows with qty <= 0 (returns) skip R2."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 0.0,
            'qty': -1.0, 'net': 0.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' not in [f['rule_code'] for f in flags]

    def test_r2_with_ratio_12_โหล(self, tmp_path):
        """R2 with unit conversion ratio=12 (โหล). cost=100/ตัว → 1200/โหล.
        eff = net/qty = 1100/10 = 110 (per โหล). flag since 110 < 1200*0.99."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        _add_unit_conversion(conn, 1, "โหล", 12)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 110.0,
            'qty': 10.0, 'net': 1100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' in [f['rule_code'] for f in flags]

    def test_r2_ratio_12_above_cost_no_flag(self, tmp_path):
        """eff=1300/โหล > 1200*0.99=1188 → no R2 flag."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        _add_unit_conversion(conn, 1, "โหล", 12)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 1300.0,
            'qty': 1.0, 'net': 1300.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R2_BELOW_COST' not in [f['rule_code'] for f in flags]

    def test_r2_no_conversion_row_triggers_r4_not_r2(self, tmp_path):
        """unit != unit_type AND no conversion row → R4, not R2 (R2 skips when no conversion)."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        # NO unit_conversions row for (1, 'โหล')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 50.0,
            'qty': 1.0, 'net': 50.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        codes = [f['rule_code'] for f in flags]
        assert 'R4_UNUSUAL_UNIT' in codes
        assert 'R2_BELOW_COST' not in codes

    def test_r2_message_contains_cost(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว", cost_price=100.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 50.0,
            'qty': 1.0, 'net': 50.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        r2 = next(f for f in flags if f['rule_code'] == 'R2_BELOW_COST')
        assert 'ทุน' in r2['message_th']
        assert r2['severity'] == 'high'


# ═══════════════════════════════════════════════════════════════════════════
# R3_PRICE_DEVIATION
# ═══════════════════════════════════════════════════════════════════════════

class TestR3PriceDeviation:
    def _seed_history(self, conn, product_id, bsn_unit, price, n_rows=3,
                      customer_code=None, batch_id=0, date_iso="2025-12-01"):
        """Seed n_rows of historical sales for R3 context (different doc per row)."""
        for i in range(n_rows):
            doc = f"IV_HIST{i+1:03d}-1"
            conn.execute("""
                INSERT INTO sales_transactions
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, customer, customer_code, qty, unit,
                     unit_price, vat_type, discount, total, net, ref_invoice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)
            """, (batch_id, date_iso, doc, doc.rsplit("-",1)[0],
                  product_id, 'ABC001', 'ชื่อดิบ', 'ลูกค้าA', customer_code,
                  1.0, bsn_unit, price, price, price))
        conn.commit()

    def test_fires_when_deviation_over_20pct(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)   # old batch, different id
        batch_id = _add_batch(conn)     # current scan batch
        _add_product(conn, 1)
        # median history = 100, scan price = 130 → +30% > 20%
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 130.0,
            'qty': 1.0, 'net': 130.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' in [f['rule_code'] for f in flags]

    def test_no_fire_at_exactly_20pct(self, tmp_path):
        """Deviation of exactly 20% should NOT fire (threshold is > 20%)."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 120.0,
            'qty': 1.0, 'net': 120.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_fires_at_20pct_plus_epsilon(self, tmp_path):
        """Deviation of 20.1% (>20%) should fire if deviation >= ฿2."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 120.1,
            'qty': 1.0, 'net': 120.1, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' in [f['rule_code'] for f in flags]

    def test_no_fire_when_abs_diff_under_2_baht(self, tmp_path):
        """Even >20% deviation: if abs(price - median) < ฿2, no flag."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        # median=5.00, scan=6.10 (+22%), but abs diff = 1.10 < ฿2 → no fire
        self._seed_history(conn, 1, "ตัว", 5.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 6.1,
            'qty': 1.0, 'net': 6.1, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_no_fire_insufficient_global_history(self, tmp_path):
        """Only 2 prior global rows (< 3 required) → no R3."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=2,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 200.0,
            'qty': 1.0, 'net': 200.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_no_fire_insufficient_docs_for_global(self, tmp_path):
        """3 rows but all in the same doc_base → only 1 distinct doc, need ≥2 docs → no R3."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        # Insert 3 lines but same doc
        for i in range(3):
            conn.execute("""
                INSERT INTO sales_transactions
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, customer, customer_code, qty, unit,
                     unit_price, vat_type, discount, total, net, ref_invoice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)
            """, (hist_batch, "2025-12-01", f"IV_SAME-{i+1}", "IV_SAME",
                  1, 'ABC001', 'ชื่อดิบ', 'ลูกค้าA', None,
                  1.0, 'ตัว', 100.0, 100.0, 100.0))
        conn.commit()
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 200.0,
            'qty': 1.0, 'net': 200.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_current_batch_excluded_from_history(self, tmp_path):
        """Rows from the CURRENT batch must not count as history."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)   # only one batch — current scan batch
        _add_product(conn, 1)
        # Seed 3 rows in the SAME (current) batch
        for i in range(3):
            conn.execute("""
                INSERT INTO sales_transactions
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, customer, customer_code, qty, unit,
                     unit_price, vat_type, discount, total, net, ref_invoice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)
            """, (batch_id, "2026-06-01", f"IV_CURR-{i+1}", "IV_CURR",
                  1, 'ABC001', 'ชื่อดิบ', 'ลูกค้าA', None,
                  1.0, 'ตัว', 100.0, 100.0, 100.0))
        conn.commit()
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 200.0,
            'qty': 1.0, 'net': 200.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        # Current-batch rows must not serve as history: no R3 fire
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_customer_specific_history_preferred(self, tmp_path):
        """If the customer has ≥2 prior lines, use customer median, not global."""
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        # global median = 100 (3 rows, no customer code → global)
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        # customer-specific: 2 rows at price 200
        for i in range(2):
            conn.execute("""
                INSERT INTO sales_transactions
                    (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                     product_name_raw, customer, customer_code, qty, unit,
                     unit_price, vat_type, discount, total, net, ref_invoice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)
            """, (hist_batch, "2025-12-01", f"IV_CUST{i+1}-1", f"IV_CUST{i+1}",
                  1, 'ABC001', 'ชื่อดิบ', 'ลูกค้าB', 'C002',
                  1.0, 'ตัว', 200.0, 200.0, 200.0))
        conn.commit()
        rr = _import_rr(db_path)
        # Scan: customer C002, price=210 (+5% from customer median 200)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV003-1', 'doc_base': 'IV003',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 210.0,
            'qty': 1.0, 'net': 210.0, 'customer': 'ลูกค้าB', 'customer_code': 'C002',
            'ref_invoice': None,
        })
        # +5% from customer median 200 — should NOT fire R3
        assert 'R3_PRICE_DEVIATION' not in [f['rule_code'] for f in flags]

    def test_r3_severity_medium(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        hist_batch = _add_batch(conn)
        batch_id = _add_batch(conn)
        _add_product(conn, 1)
        self._seed_history(conn, 1, "ตัว", 100.0, n_rows=3,
                           customer_code=None, batch_id=hist_batch)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 99, 'batch_id': batch_id, 'doc_no': 'IV002-1', 'doc_base': 'IV002',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 130.0,
            'qty': 1.0, 'net': 130.0, 'customer': 'ลูกค้าใหม่', 'customer_code': 'C999',
            'ref_invoice': None,
        })
        r3 = next(f for f in flags if f['rule_code'] == 'R3_PRICE_DEVIATION')
        assert r3['severity'] == 'medium'


# ═══════════════════════════════════════════════════════════════════════════
# R4_UNUSUAL_UNIT
# ═══════════════════════════════════════════════════════════════════════════

class TestR4UnusualUnit:
    def test_fires_when_unit_differs_and_no_conversion(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว")
        # No unit_conversions row for (1, 'โหล')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 1200.0,
            'qty': 1.0, 'net': 1200.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R4_UNUSUAL_UNIT' in [f['rule_code'] for f in flags]

    def test_no_fire_when_unit_matches_unit_type(self, tmp_path):
        """unit == unit_type → no R4."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว")
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 150.0,
            'qty': 1.0, 'net': 150.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R4_UNUSUAL_UNIT' not in [f['rule_code'] for f in flags]

    def test_no_fire_when_conversion_exists(self, tmp_path):
        """unit != unit_type but unit_conversions row exists → no R4."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว")
        _add_unit_conversion(conn, 1, "โหล", 12)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 1300.0,
            'qty': 1.0, 'net': 1300.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R4_UNUSUAL_UNIT' not in [f['rule_code'] for f in flags]

    def test_no_fire_unmapped_product(self, tmp_path):
        """product_id IS NULL → R1 fires, R4 should NOT (no unit_type to compare)."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': None, 'bsn_code': 'NONE',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R4_UNUSUAL_UNIT' not in [f['rule_code'] for f in flags]

    def test_r4_severity_high(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, unit_type="ตัว")
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        r4 = next(f for f in flags if f['rule_code'] == 'R4_UNUSUAL_UNIT')
        assert r4['severity'] == 'high'
        assert 'โหล' in r4['message_th']
        assert 'ตัว' in r4['message_th']


# ═══════════════════════════════════════════════════════════════════════════
# R5_PROMO_MISMATCH
# ═══════════════════════════════════════════════════════════════════════════

class TestR5PromoMismatch:
    def test_fires_percent_promo_sold_at_full_price(self, tmp_path):
        """Active percent promo: base=100, discount=20% → expected 80.
        Sold at 100 (full price, not applying promo) → flag 'ไม่ได้ใช้โปร'."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' in [f['rule_code'] for f in flags]

    def test_fires_percent_promo_wrong_price(self, tmp_path):
        """Sold at 70 but expected 80 (percent 20% of base 100) — more than 1% off."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 70.0,
            'qty': 1.0, 'net': 70.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' in [f['rule_code'] for f in flags]

    def test_no_fire_percent_promo_price_matches(self, tmp_path):
        """Sold at exactly 80 (within 1% tolerance) → no flag."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 80.0,
            'qty': 1.0, 'net': 80.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_fires_fixed_promo_wrong_price(self, tmp_path):
        """Fixed promo: discount_value = FINAL price = 75. Sold at 100 → flag."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'fixed', 75.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' in [f['rule_code'] for f in flags]

    def test_no_fire_fixed_promo_matches(self, tmp_path):
        """Fixed promo: discount_value = 75 = final price. Sold at 75 → no flag."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'fixed', 75.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 75.0,
            'qty': 1.0, 'net': 75.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_no_fire_bundle_promo_skipped(self, tmp_path):
        """Bundle promos are skipped by R5 per spec."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'bundle', None,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 999.0,
            'qty': 1.0, 'net': 999.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_no_fire_gift_promo_skipped(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'gift', None,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 999.0,
            'qty': 1.0, 'net': 999.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_no_fire_mixed_promo_skipped(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'mixed', None,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 999.0,
            'qty': 1.0, 'net': 999.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_no_fire_no_active_promo(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        # Promo expired before the sale date
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-05-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_r5_uses_date_iso_not_today(self, tmp_path):
        """Date-parameterized: promo active on date_iso=2025-12-01 but expired by today."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        # Promo was active in Dec 2025, now expired
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2025-11-01', date_end='2025-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2025-12-15', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        # Promo was active on 2025-12-15 → should flag
        assert 'R5_PROMO_MISMATCH' in [f['rule_code'] for f in flags]

    def test_r5_tier_pass_no_flag(self, tmp_path):
        """If unit_price matches a product_price_tiers row → no R5 flag."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        # expected promo price = 80; but tier at 90
        _add_price_tier(conn, 1, "1 โหล", 90.0)
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 90.0,
            'qty': 12.0, 'net': 1080.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_r5_full_price_flag_message(self, tmp_path):
        """Sold at base_sell_price while promo active → flag with 'ไม่ได้ใช้โปร' hint."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        r5 = next(f for f in flags if f['rule_code'] == 'R5_PROMO_MISMATCH')
        assert 'ไม่ได้ใช้โปร' in r5['message_th']
        assert r5['severity'] == 'medium'

    def test_r5_with_ratio_unit_conversion(self, tmp_path):
        """R5: percent promo, unit=โหล ratio=12. base=100, promo=20%off → expected 80/ตัว=960/โหล."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_unit_conversion(conn, 1, "โหล", 12)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        # unit_price=960 = 80*12 → matches expected → no flag
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'IV001-1', 'doc_base': 'IV001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'โหล', 'unit_price': 960.0,
            'qty': 1.0, 'net': 960.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': None,
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]

    def test_r5_sr_row_skip(self, tmp_path):
        """SR rows skip price rules including R5."""
        db_path, conn = _make_db(tmp_path)
        batch_id = _add_batch(conn)
        _add_product(conn, 1, base_sell_price=100.0)
        _add_promo(conn, 1, 'percent', 20.0,
                   date_start='2026-01-01', date_end='2026-12-31')
        rr = _import_rr(db_path)
        flags = rr._check_row_rules(conn, {
            'id': 1, 'batch_id': batch_id, 'doc_no': 'SR001-1', 'doc_base': 'SR001',
            'date_iso': '2026-06-01', 'product_id': 1, 'bsn_code': 'ABC001',
            'product_name_raw': 'ชื่อดิบ', 'unit': 'ตัว', 'unit_price': 100.0,
            'qty': 1.0, 'net': 100.0, 'customer': 'ลูกค้า', 'customer_code': 'C001',
            'ref_invoice': 'IV001',
        })
        assert 'R5_PROMO_MISMATCH' not in [f['rule_code'] for f in flags]
