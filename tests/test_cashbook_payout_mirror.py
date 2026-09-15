"""cashbook_payout_mirror.mirror_platform — issue #533.

Mirrors marketplace_payouts (platform, deposit_date >= 2026-01-01) into
locked income rows on that platform's cashbook account (lazada -> LEX,
shopee -> SPX). Insert what's missing, delete what's gone, touch nothing
else — in particular, NEVER touch a manual row (payout_platform IS NULL),
even one whose amount/date coincide with a real payout.

Tests force the state they need (delete any pre-existing marketplace_payouts
/ payout-sourced cashbook rows first) rather than inheriting the shared dev
DB's content, per .claude/rules/verification-discipline.md.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database
import cashbook_payout_mirror as mirror


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # Force a clean slate: no inherited payouts, no inherited payout-sourced
    # cashbook rows. Manual rows are left alone (they're what we must prove
    # we never touch).
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM cashbook_transactions WHERE payout_platform IS NOT NULL")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _account_id(conn, code):
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    assert row is not None, f"expected seeded cashbook account {code}"
    return row['id']


def _seed_payout(conn, platform, deposit_date, amount, n_orders=1, status='reconciled'):
    conn.execute(
        "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders, status)"
        " VALUES (?, ?, ?, ?, ?)",
        (platform, deposit_date, amount, n_orders, status),
    )


def _payout_rows(conn, account_id):
    return conn.execute(
        "SELECT * FROM cashbook_transactions WHERE account_id=? AND payout_platform IS NOT NULL"
        " ORDER BY payout_deposit_date, payout_occurrence",
        (account_id,),
    ).fetchall()


# ── First run: insert ───────────────────────────────────────────────────────

def test_first_run_inserts_one_row_per_payout_with_correct_shape(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=12)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 1, 'deleted': 0, 'unchanged': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 1
    r = rows[0]
    assert r['direction'] == 'income'
    assert r['category'] == 'ยอดขายของ'
    assert r['txn_date'] == '2026-03-10'
    assert float(r['amount']) == 3596.00
    assert r['description'] == 'Shopee โอนเงิน (12 ออเดอร์)'
    assert r['created_by'] == 'ระบบ'
    assert r['payout_platform'] == 'shopee'
    assert r['payout_deposit_date'] == '2026-03-10'
    assert float(r['payout_amount']) == 3596.00
    assert r['payout_occurrence'] == 1


def test_lazada_maps_to_lex(conn):
    lex = _account_id(conn, 'LEX')
    _seed_payout(conn, 'lazada', '2026-02-01', 1000.00, n_orders=3)
    conn.commit()

    mirror.mirror_platform(conn, 'lazada')

    rows = _payout_rows(conn, lex)
    assert len(rows) == 1
    assert rows[0]['description'] == 'Lazada โอนเงิน (3 ออเดอร์)'


def test_payout_before_2026_excluded(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2025-12-31', 500.00)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 0, 'deleted': 0, 'unchanged': 0}
    assert _payout_rows(conn, spx) == []


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_second_run_with_no_payout_change_is_a_noop(conn):
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()

    mirror.mirror_platform(conn, 'shopee')
    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 0, 'deleted': 0, 'unchanged': 1}


# ── Duplicated (platform, date, amount) payout — real prod case, mig 108 ───

def test_duplicate_payout_same_key_gets_two_rows_by_occurrence(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-04-01', 3596.00, n_orders=5)
    _seed_payout(conn, 'shopee', '2026-04-01', 3596.00, n_orders=7)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 2, 'deleted': 0, 'unchanged': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 2
    assert [r['payout_occurrence'] for r in rows] == [1, 2]
    assert {r['description'] for r in rows} == {
        'Shopee โอนเงิน (5 ออเดอร์)', 'Shopee โอนเงิน (7 ออเดอร์)',
    }

    # idempotent: a second run with the same two payouts changes nothing
    result2 = mirror.mirror_platform(conn, 'shopee')
    assert result2 == {'inserted': 0, 'deleted': 0, 'unchanged': 2}
    assert len(_payout_rows(conn, spx)) == 2


# ── Rebuild: payout disappears -> row removed ───────────────────────────────

def test_payout_removed_in_rebuild_deletes_its_cashbook_row(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    assert len(_payout_rows(conn, spx)) == 1

    # Simulate a reconcile rebuild that no longer produces this payout.
    conn.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 0, 'deleted': 1, 'unchanged': 0}
    assert _payout_rows(conn, spx) == []


# ── Rebuild: payout amount changes -> old row gone, new row present ────────

def test_payout_amount_change_replaces_the_row(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=5)
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    old_id = _payout_rows(conn, spx)[0]['id']

    conn.execute("UPDATE marketplace_payouts SET amount=3700.00, n_orders=6 WHERE platform='shopee'")
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 1, 'deleted': 1, 'unchanged': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 1
    assert rows[0]['id'] != old_id
    assert float(rows[0]['amount']) == 3700.00
    assert rows[0]['description'] == 'Shopee โอนเงิน (6 ออเดอร์)'


# ── Manual rows are never touched ───────────────────────────────────────────

def test_manual_row_on_lex_survives_every_mirror_run(conn):
    lex = _account_id(conn, 'LEX')
    cur = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-01-01', 'income', 'เงินทุน/เงินโอน', 999999, 'ยอดยกมา', 'พุธ')",
        (lex,),
    )
    manual_id = cur.lastrowid
    _seed_payout(conn, 'lazada', '2026-02-01', 1000.00)
    conn.commit()

    for _ in range(2):
        mirror.mirror_platform(conn, 'lazada')

    row = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE id=?", (manual_id,)
    ).fetchone()
    assert row is not None, "manual row must never be deleted"
    assert row['amount'] == 999999
    assert row['payout_platform'] is None, "manual row must never be adopted/linked"


def test_manual_row_matching_a_payouts_amount_and_date_does_not_block_insert(conn):
    """A manual row that happens to coincide with a real payout (amount +
    date) is NOT the one-time-conversion case here — the mirror must still
    insert its own payout-sourced row alongside it, leaving both visible."""
    spx = _account_id(conn, 'SPX')
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-03-10', 'income', 'ยอดขายของ', 3596.00, 'คีย์มือ', 'พุธ')",
        (spx,),
    )
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 1, 'deleted': 0, 'unchanged': 0}

    all_rows = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE account_id=? AND amount=3596.00",
        (spx,),
    ).fetchall()
    assert len(all_rows) == 2, "manual row untouched + one new payout-sourced row"
    manual = [r for r in all_rows if r['payout_platform'] is None]
    mirrored = [r for r in all_rows if r['payout_platform'] is not None]
    assert len(manual) == 1 and manual[0]['description'] == 'คีย์มือ'
    assert len(mirrored) == 1


# ── Missing / inactive destination account ──────────────────────────────────

def test_missing_account_raises_and_inserts_nothing(conn):
    conn.execute("UPDATE cashbook_accounts SET is_active=0 WHERE code='SPX'")
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()

    with pytest.raises(mirror.CashbookPayoutMirrorError):
        mirror.mirror_platform(conn, 'shopee')

    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payout_platform='shopee'"
    ).fetchone()[0]
    assert n == 0


def test_unknown_platform_raises(conn):
    with pytest.raises(mirror.CashbookPayoutMirrorError):
        mirror.mirror_platform(conn, 'tiktok')
