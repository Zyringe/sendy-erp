"""scripts/convert_legacy_cashbook_payout_rows.py — the ONE-TIME conversion
of pre-existing hand-keyed LEX/SPX rows into payout-sourced rows (issue
#533 "Existing rows"). Loaded by path (see tests/test_promo_price_owner.py
for why: a name collision between inventory_app/ and scripts/ made plain
`import` order-dependent until #489 — loading by path can't be fooled by
collection order).
"""
import importlib.util as _ilu
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts', 'convert_legacy_cashbook_payout_rows.py')
_spec = _ilu.spec_from_file_location('convert_legacy_cashbook_payout_rows_under_test', _SCRIPT)
convert = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(convert)
assert convert.__file__.endswith(os.path.join('scripts', 'convert_legacy_cashbook_payout_rows.py'))


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM cashbook_transactions WHERE payout_platform IS NOT NULL")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _account_id(conn, code):
    return conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()['id']


def _seed_payout(conn, platform, deposit_date, amount, n_orders=1):
    conn.execute(
        "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders, status)"
        " VALUES (?, ?, ?, ?, 'reconciled')",
        (platform, deposit_date, amount, n_orders),
    )


def _seed_manual(conn, account_id, txn_date, amount, description='คีย์มือ'):
    cur = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, ?, 'income', 'ยอดขายของ', ?, ?, 'พุธ')",
        (account_id, txn_date, amount, description),
    )
    return cur.lastrowid


# ── Within-window match converts ────────────────────────────────────────────

def test_manual_row_within_window_converts(conn):
    lex = _account_id(conn, 'LEX')
    manual_id = _seed_manual(conn, lex, '2026-02-03', 1000.00)  # 2 days after deposit
    _seed_payout(conn, 'lazada', '2026-02-01', 1000.00, n_orders=4)
    conn.commit()

    conversions, ambiguous, unmatched = convert.find_conversions(conn, 'lazada')
    assert ambiguous == [] and unmatched == []
    assert len(conversions) == 1
    c = conversions[0]
    assert c['txn_id'] == manual_id
    assert c['deposit_date'] == '2026-02-01'
    assert c['description'] == 'Lazada โอนเงิน (4 ออเดอร์)'

    convert.apply_conversions(conn, 'lazada', conversions)
    conn.commit()

    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id=?", (manual_id,)).fetchone()
    assert row['txn_date'] == '2026-02-01'
    assert row['category'] == 'ยอดขายของ'
    assert row['description'] == 'Lazada โอนเงิน (4 ออเดอร์)'
    assert row['created_by'] == 'ระบบ'
    assert row['payout_platform'] == 'lazada'
    assert row['payout_deposit_date'] == '2026-02-01'
    assert float(row['payout_amount']) == 1000.00
    assert row['payout_occurrence'] == 1


def test_conversion_is_idempotent_second_pass_finds_nothing(conn):
    lex = _account_id(conn, 'LEX')
    _seed_manual(conn, lex, '2026-02-03', 1000.00)
    _seed_payout(conn, 'lazada', '2026-02-01', 1000.00, n_orders=4)
    conn.commit()

    conversions, _, _ = convert.find_conversions(conn, 'lazada')
    convert.apply_conversions(conn, 'lazada', conversions)
    conn.commit()

    conversions2, ambiguous2, unmatched2 = convert.find_conversions(conn, 'lazada')
    assert conversions2 == []
    assert ambiguous2 == []
    assert unmatched2 == []  # the payout is now already-mirrored, not "unmatched"


# ── Outside-window match must NOT convert ───────────────────────────────────

def test_manual_row_outside_window_does_not_convert(conn):
    spx = _account_id(conn, 'SPX')
    manual_id = _seed_manual(conn, spx, '2026-03-15', 3596.00)  # 5 days after deposit
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=12)
    conn.commit()

    conversions, ambiguous, unmatched = convert.find_conversions(conn, 'shopee')
    assert conversions == []
    assert ambiguous == []
    assert len(unmatched) == 1

    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id=?", (manual_id,)).fetchone()
    assert row['payout_platform'] is None
    assert row['txn_date'] == '2026-03-15', "the out-of-window manual row must stay untouched"


# ── An unrelated manual row (different amount) is simply not seen ──────────

def test_unrelated_manual_row_is_left_alone(conn):
    spx = _account_id(conn, 'SPX')
    other_id = _seed_manual(conn, spx, '2026-03-04', 550.00, description='ของจริงไม่เกี่ยว')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=12)
    conn.commit()

    conversions, _, _ = convert.find_conversions(conn, 'shopee')
    convert.apply_conversions(conn, 'shopee', conversions)
    conn.commit()

    row = conn.execute("SELECT * FROM cashbook_transactions WHERE id=?", (other_id,)).fetchone()
    assert row['amount'] == 550.00 and row['payout_platform'] is None


# ── Ambiguous match: more than one candidate — skip, report, never guess ───

def test_two_candidates_for_one_payout_is_ambiguous_and_skipped(conn):
    spx = _account_id(conn, 'SPX')
    id_a = _seed_manual(conn, spx, '2026-03-10', 3596.00, description='A')
    id_b = _seed_manual(conn, spx, '2026-03-11', 3596.00, description='B')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=12)
    conn.commit()

    conversions, ambiguous, unmatched = convert.find_conversions(conn, 'shopee')
    assert conversions == []
    assert len(ambiguous) == 1
    assert set(ambiguous[0]['candidate_ids']) == {id_a, id_b}

    for tid in (id_a, id_b):
        row = conn.execute("SELECT payout_platform FROM cashbook_transactions WHERE id=?", (tid,)).fetchone()
        assert row['payout_platform'] is None, "an ambiguous match must never be guessed"


# ── main(): live mode aborts the whole run on any ambiguity ─────────────────

def test_live_mode_raises_and_writes_nothing_when_ambiguous(conn, tmp_db):
    spx = _account_id(conn, 'SPX')
    id_a = _seed_manual(conn, spx, '2026-03-10', 3596.00)
    id_b = _seed_manual(conn, spx, '2026-03-11', 3596.00)
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=12)
    conn.commit()
    conn.close()

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ['convert_legacy_cashbook_payout_rows.py', 'live', '--db', tmp_db, '--yes-really']
        with pytest.raises(RuntimeError, match='ambiguous'):
            convert.main()
    finally:
        sys.argv = old_argv

    check = sqlite3.connect(tmp_db)
    try:
        for tid in (id_a, id_b):
            row = check.execute(
                "SELECT payout_platform FROM cashbook_transactions WHERE id=?", (tid,)
            ).fetchone()
            assert row[0] is None, "an aborted live run must leave every row untouched"
    finally:
        check.close()
