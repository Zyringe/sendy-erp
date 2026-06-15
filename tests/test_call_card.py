"""Tests for call_card.py — Phase 2B TDD.

Covers:
  - Pure helpers: call_status, elapsed_th
  - Log/CRM round-trip helpers (with a temp DB that applies mig 103)
  - get_call_list smoke test (keys present, no N+1)
"""
import datetime as dt
import os
import sqlite3

import pytest

import call_card as cc

# ── mig-103 fixture ──────────────────────────────────────────────────────────

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_103 = os.path.join(REPO, "data", "migrations", "103_customer_call_card.sql")


def _build_mig103_db(path):
    """Create a minimal DB that has the mig-103 tables + the minimum schema
    needed for the call_card module (ar_followup_log for _resolve_target,
    sales_transactions for get_call_list, etc.)."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # ── tables needed for _resolve_target ────────────────────────────────────
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            code TEXT PRIMARY KEY,
            name TEXT,
            salesperson TEXT,
            zone TEXT,
            address TEXT,
            phone TEXT,
            tax_id TEXT,
            credit_days INTEGER,
            contact TEXT,
            region_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS ar_followup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer TEXT,
            customer_code TEXT,
            log_date TEXT,
            result TEXT,
            notes TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS express_ar_outstanding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT,
            customer_code TEXT,
            outstanding REAL,
            snapshot_date_iso TEXT,
            entity TEXT,
            doc_date_iso TEXT,
            doc_no TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT,
            row_id INTEGER,
            action TEXT,
            changed_fields TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS applied_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE,
            applied_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS sales_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            date_iso TEXT,
            doc_no TEXT,
            product_id INTEGER,
            bsn_code TEXT,
            product_name_raw TEXT,
            customer TEXT,
            customer_code TEXT,
            qty REAL,
            unit TEXT,
            unit_price REAL,
            vat_type INTEGER,
            discount TEXT,
            total REAL,
            net REAL,
            created_at TEXT,
            synced_to_stock INTEGER,
            doc_base TEXT,
            ref_invoice TEXT
        );
    """)
    conn.commit()

    # Apply mig 103 (creates customer_call_log + customer_crm + audit triggers)
    with open(MIG_103, encoding="utf-8") as f:
        conn.executescript(f.read())

    return conn


@pytest.fixture
def mig103_conn(tmp_path):
    path = str(tmp_path / "test_call.db")
    conn = _build_mig103_db(path)
    yield conn
    conn.close()


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_status_never():
    assert cc.call_status(None, 365)[0] == 'never'


def test_status_recent_under_target():
    last = (dt.date(2026, 6, 14) - dt.timedelta(days=30)).isoformat()
    assert cc.call_status(last, 365, today=dt.date(2026, 6, 14))[0] == 'recent'


def test_status_due_over_target():
    last = (dt.date(2026, 6, 14) - dt.timedelta(days=400)).isoformat()
    st, days = cc.call_status(last, 365, today=dt.date(2026, 6, 14))
    assert st == 'due' and days == 400


def test_status_due_exactly_at_target():
    last = (dt.date(2026, 6, 14) - dt.timedelta(days=365)).isoformat()
    st, days = cc.call_status(last, 365, today=dt.date(2026, 6, 14))
    assert st == 'due' and days == 365


def test_elapsed_th_days():
    assert cc.elapsed_th(5) == "5 วันก่อน"


def test_elapsed_th_months():
    result = cc.elapsed_th(40)
    assert "เดือนก่อน" in result


def test_elapsed_th_years():
    assert "ปี" in cc.elapsed_th(430)


def test_elapsed_th_over_1y_no_months():
    # 365 days exactly = 1 ปี 0 เดือน → show "1 ปีก่อน"
    result = cc.elapsed_th(365)
    assert "ปีก่อน" in result


def test_status_label_keys():
    assert set(cc.STATUS_LABEL.keys()) == {'recent', 'due', 'never'}


def test_default_call_target_days():
    assert cc.DEFAULT_CALL_TARGET_DAYS == 365


# ── Log/CRM round-trip ───────────────────────────────────────────────────────

def test_add_log_and_get_log(mig103_conn):
    cc.add_log(mig103_conn, 'C001', 'note', 'hello', 'sanchai')
    rows = cc.get_log(mig103_conn, 'C001')
    assert len(rows) == 1
    assert rows[0]['body'] == 'hello'
    assert rows[0]['kind'] == 'note'
    assert rows[0]['deleted_at'] is None


def test_mark_called_creates_call_row(mig103_conn):
    cc.mark_called(mig103_conn, 'C001', 'sanchai')
    rows = cc.get_log(mig103_conn, 'C001')
    assert any(r['kind'] == 'call' for r in rows)


def test_last_called_at_returns_today(mig103_conn):
    cc.mark_called(mig103_conn, 'C001', 'sanchai')
    result = cc.last_called_at(mig103_conn, 'C001')
    assert result is not None
    # Should be today's date
    today_str = dt.date.today().isoformat()
    assert result[:10] == today_str


def test_last_called_at_none_when_no_call(mig103_conn):
    cc.add_log(mig103_conn, 'C001', 'note', 'just a note', 'user')
    result = cc.last_called_at(mig103_conn, 'C001')
    assert result is None


def test_soft_delete_log_hides_from_get_log(mig103_conn):
    cc.add_log(mig103_conn, 'C001', 'note', 'to delete', 'sanchai')
    rows = cc.get_log(mig103_conn, 'C001')
    assert len(rows) == 1
    log_id = rows[0]['id']

    cc.soft_delete_log(mig103_conn, log_id, 'sanchai')
    rows_after = cc.get_log(mig103_conn, 'C001')
    assert len(rows_after) == 0


def test_soft_delete_only_own_rows(mig103_conn):
    """soft_delete_log requires matching created_by — other user's row stays."""
    cc.add_log(mig103_conn, 'C001', 'note', 'owner only', 'sanchai')
    rows = cc.get_log(mig103_conn, 'C001')
    log_id = rows[0]['id']

    cc.soft_delete_log(mig103_conn, log_id, 'other_user')
    rows_after = cc.get_log(mig103_conn, 'C001')
    assert len(rows_after) == 1  # not deleted (wrong user)


def test_get_crm_none_when_missing(mig103_conn):
    assert cc.get_crm(mig103_conn, 'C001') is None


def test_upsert_crm_creates_row(mig103_conn):
    cc.upsert_crm(mig103_conn, 'C001', 'sanchai', tags='VIP', call_target_days=90)
    row = cc.get_crm(mig103_conn, 'C001')
    assert row is not None
    assert row['tags'] == 'VIP'
    assert row['call_target_days'] == 90


def test_upsert_crm_updates_row(mig103_conn):
    cc.upsert_crm(mig103_conn, 'C001', 'sanchai', tags='VIP')
    cc.upsert_crm(mig103_conn, 'C001', 'sanchai', tags='REGULAR')
    row = cc.get_crm(mig103_conn, 'C001')
    assert row['tags'] == 'REGULAR'


def test_target_days_for_uses_crm(mig103_conn):
    cc.upsert_crm(mig103_conn, 'C001', 'sanchai', call_target_days=30)
    row = cc.get_crm(mig103_conn, 'C001')
    assert cc.target_days_for(row) == 30


def test_target_days_for_default_when_none():
    assert cc.target_days_for(None) == cc.DEFAULT_CALL_TARGET_DAYS


def test_target_days_for_default_when_crm_has_none(mig103_conn):
    cc.upsert_crm(mig103_conn, 'C001', 'sanchai', tags='x')
    row = cc.get_crm(mig103_conn, 'C001')
    assert cc.target_days_for(row) == cc.DEFAULT_CALL_TARGET_DAYS


# ── get_call_list smoke test ─────────────────────────────────────────────────

EXPECTED_KEYS = {
    'customer_code', 'name', 'province', 'region',
    'last_buy', 'spend', 'call_status', 'call_days',
    'last_called', 'badges',
}
EXPECTED_BADGE_KEYS = {'ar', 'quiet', 'special'}


def test_call_list_smoke(mig103_conn):
    """get_call_list returns rows with documented keys against a seeded temp DB."""
    conn = mig103_conn
    # Seed two customers + sales rows
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านทดสอบ','กรุงเทพมหานคร')"
    )
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C002','ร้านทดสอบ 2','ขอนแก่น 40000')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ('2026-01-15', 'IV001', 'ร้านทดสอบ', 'C001', 10, 'ตัว', 100, 0, 1000, 1),
            ('2026-03-20', 'IV002', 'ร้านทดสอบ', 'C001', 5, 'ตัว', 200, 0, 1000, 2),
            ('2026-02-10', 'IV003', 'ร้านทดสอบ 2', 'C002', 3, 'ตัว', 150, 0, 450, 1),
        ]
    )
    conn.commit()

    rows = cc.get_call_list(conn)
    assert len(rows) >= 2, f"expected ≥2 rows, got {len(rows)}"

    for row in rows:
        missing = EXPECTED_KEYS - set(row.keys())
        assert not missing, f"row missing keys: {missing}"
        assert EXPECTED_BADGE_KEYS == set(row['badges'].keys()), \
            f"badges missing keys: {set(row['badges'].keys())}"


def test_call_list_region_filter(mig103_conn):
    """Region filter reduces rows."""
    conn = mig103_conn
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านทดสอบ','กรุงเทพมหานคร')"
    )
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C002','ร้านอีสาน','ขอนแก่น 40000')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ('2026-01-15', 'IV001', 'ร้านทดสอบ', 'C001', 1, 'ตัว', 100, 0, 100, 1),
            ('2026-01-15', 'IV002', 'ร้านอีสาน', 'C002', 1, 'ตัว', 100, 0, 100, 1),
        ]
    )
    conn.commit()

    bkk_rows = cc.get_call_list(conn, region='กรุงเทพฯ/ปริมณฑล')
    isan_rows = cc.get_call_list(conn, region='ภาคอีสาน')
    assert all(r['region'] == 'กรุงเทพฯ/ปริมณฑล' for r in bkk_rows)
    assert all(r['region'] == 'ภาคอีสาน' for r in isan_rows)


def test_call_list_call_status_filter(mig103_conn):
    """call=never filter returns only customers who have never been called."""
    conn = mig103_conn
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านทดสอบ','กรุงเทพมหานคร')"
    )
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C002','ร้านทดสอบ 2','กรุงเทพมหานคร')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ('2026-01-15', 'IV001', 'ร้านทดสอบ', 'C001', 1, 'ตัว', 100, 0, 100, 1),
            ('2026-01-15', 'IV002', 'ร้านทดสอบ 2', 'C002', 1, 'ตัว', 100, 0, 100, 1),
        ]
    )
    conn.commit()

    # Mark C001 as called
    cc.mark_called(conn, 'C001', 'sanchai')

    never_rows = cc.get_call_list(conn, call='never')
    codes = [r['customer_code'] for r in never_rows]
    assert 'C001' not in codes
    assert 'C002' in codes


def test_call_list_spend_window(mig103_conn):
    """spend_window='6m' only counts recent sales."""
    conn = mig103_conn
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านทดสอบ','กรุงเทพมหานคร')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            # Old sale (>6m ago)
            ('2020-01-01', 'IV_OLD', 'ร้านทดสอบ', 'C001', 1, 'ตัว', 100, 0, 5000, 1),
            # Recent sale
            ('2026-05-01', 'IV_NEW', 'ร้านทดสอบ', 'C001', 1, 'ตัว', 100, 0, 100, 1),
        ]
    )
    conn.commit()

    all_rows = cc.get_call_list(conn, spend_window='all')
    six_rows = cc.get_call_list(conn, spend_window='6m')
    assert len(all_rows) >= 1 and len(six_rows) >= 1
    all_spend = next(r['spend'] for r in all_rows if r['customer_code'] == 'C001')
    six_spend = next(r['spend'] for r in six_rows if r['customer_code'] == 'C001')
    assert all_spend > six_spend, "6m window should have lower spend than all-time"


def test_call_list_universe_stable_across_windows(mig103_conn):
    """Customers with ONLY old sales must appear in ALL spend_windows
    (with spend=0 in the narrow window). Universe must not shrink.

    This is the core correctness check: a win-back target (gone quiet)
    must never disappear from the call list just because spend_window narrows.
    """
    conn = mig103_conn
    # C001 has a recent sale, C002 has ONLY an old sale (>2y ago)
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านใหม่','กรุงเทพมหานคร')"
    )
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C002','ร้านเก่า','กรุงเทพมหานคร')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ('2026-05-01', 'IV001', 'ร้านใหม่', 'C001', 1, 'ตัว', 100, 0, 500, 1),
            ('2020-01-01', 'IV002', 'ร้านเก่า', 'C002', 1, 'ตัว', 100, 0, 1000, 1),
        ]
    )
    conn.commit()

    rows_all = cc.get_call_list(conn, spend_window='all')
    rows_1y  = cc.get_call_list(conn, spend_window='1y')
    rows_6m  = cc.get_call_list(conn, spend_window='6m')

    codes_all = {r['customer_code'] for r in rows_all}
    codes_1y  = {r['customer_code'] for r in rows_1y}
    codes_6m  = {r['customer_code'] for r in rows_6m}

    # Both customers must appear in every window
    assert 'C002' in codes_all, "C002 missing from spend_window='all'"
    assert 'C002' in codes_1y,  "C002 missing from spend_window='1y' (win-back target disappeared)"
    assert 'C002' in codes_6m,  "C002 missing from spend_window='6m' (win-back target disappeared)"

    # Universe size must be identical across windows (spend_window doesn't filter rows)
    assert len(rows_all) == len(rows_1y) == len(rows_6m), (
        f"Universe changed across windows: all={len(rows_all)} 1y={len(rows_1y)} 6m={len(rows_6m)}"
    )

    # C002's spend should be 0 for narrow windows, >0 for 'all'
    c002_all = next(r for r in rows_all if r['customer_code'] == 'C002')
    c002_6m  = next(r for r in rows_6m  if r['customer_code'] == 'C002')
    assert c002_all['spend'] > 0, "C002 all-time spend should be >0"
    assert c002_6m['spend'] == 0.0, "C002 6m spend should be 0 (no recent sales)"

    # last_buy comes from all-time query, independent of window
    assert c002_6m['last_buy'] == '2020-01-01', \
        "last_buy must be all-time even in narrow spend_window"


def test_call_list_excludes_marketplace_customers(mig103_conn):
    """หน้าร้านS/B/L are NOT call targets and must be excluded from the universe."""
    conn = mig103_conn
    conn.execute(
        "INSERT INTO customers(code, name, address) VALUES ('C001','ร้านจริง','กรุงเทพมหานคร')"
    )
    conn.executemany(
        "INSERT INTO sales_transactions(date_iso, doc_no, customer, customer_code, qty, unit, "
        "unit_price, vat_type, net, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ('2026-05-01', 'IV001', 'ร้านจริง',   'C001', 1, 'ตัว', 100, 0, 500, 1),
            ('2026-05-01', 'IV002', 'หน้าร้านS',   '',    1, 'ตัว', 100, 0, 300, 1),
            ('2026-05-01', 'IV003', 'หน้าร้านL',   '',    1, 'ตัว', 100, 0, 200, 1),
            ('2026-05-01', 'IV004', 'หน้าร้านB',   '',    1, 'ตัว', 100, 0, 100, 1),
        ]
    )
    conn.commit()

    rows = cc.get_call_list(conn)
    codes = {r['customer_code'] for r in rows}
    names = {r['name'] for r in rows}

    assert 'C001' in codes, "Real customer must appear"
    for mkt in ('หน้าร้านS', 'หน้าร้านL', 'หน้าร้านB'):
        assert mkt not in codes, f"Marketplace account {mkt} must be excluded"
        assert mkt not in names, f"Marketplace account {mkt} must be excluded (name check)"
