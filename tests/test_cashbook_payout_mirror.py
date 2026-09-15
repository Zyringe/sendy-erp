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
    assert result == {'inserted': 1, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}

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
    assert result == {'inserted': 0, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}
    assert _payout_rows(conn, spx) == []


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_second_run_with_no_payout_change_is_a_noop(conn):
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()

    mirror.mirror_platform(conn, 'shopee')
    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 0, 'deleted': 0, 'updated': 0, 'unchanged': 1, 'skipped_conflicts': 0}


# ── Duplicated (platform, date, amount) payout — real prod case, mig 108 ───

def test_duplicate_payout_same_key_gets_two_rows_by_occurrence(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-04-01', 3596.00, n_orders=5)
    _seed_payout(conn, 'shopee', '2026-04-01', 3596.00, n_orders=7)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 2, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 2
    assert [r['payout_occurrence'] for r in rows] == [1, 2]
    assert {r['description'] for r in rows} == {
        'Shopee โอนเงิน (5 ออเดอร์)', 'Shopee โอนเงิน (7 ออเดอร์)',
    }

    # idempotent: a second run with the same two payouts changes nothing
    result2 = mirror.mirror_platform(conn, 'shopee')
    assert result2 == {'inserted': 0, 'deleted': 0, 'updated': 0, 'unchanged': 2, 'skipped_conflicts': 0}
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
    assert result == {'inserted': 0, 'deleted': 1, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}
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
    assert result == {'inserted': 1, 'deleted': 1, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 1
    assert rows[0]['id'] != old_id
    assert float(rows[0]['amount']) == 3700.00
    assert rows[0]['description'] == 'Shopee โอนเงิน (6 ออเดอร์)'


# ── Rebuild: n_orders changes with amount+date UNCHANGED -> UPDATE in place
# (found in review: the natural key doesn't include n_orders, so a plain
# insert/delete diff would leave the row's description permanently stale —
# real scenario: a Lazada statement's settled AMOUNT is authoritative and
# fixed once banked (lazada_statement_settlement, INSERT OR REPLACE keyed by
# statement), but its order membership (n_orders) is read fresh from
# marketplace_wallet_txns every rebuild and grows if more order rows for
# that already-settled statement get uploaded later.) ────────────────────

def test_n_orders_change_alone_updates_the_row_in_place(conn):
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=5)
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    same_id = _payout_rows(conn, spx)[0]['id']

    conn.execute("UPDATE marketplace_payouts SET n_orders=12 WHERE platform='shopee'")
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 0, 'deleted': 0, 'updated': 1, 'unchanged': 0, 'skipped_conflicts': 0}

    rows = _payout_rows(conn, spx)
    assert len(rows) == 1
    assert rows[0]['id'] == same_id, "must UPDATE the existing row, not replace it"
    assert rows[0]['description'] == 'Shopee โอนเงิน (12 ออเดอร์)'
    assert float(rows[0]['amount']) == 3596.00

    # idempotent: a second run with no further change updates nothing
    result2 = mirror.mirror_platform(conn, 'shopee')
    assert result2 == {'inserted': 0, 'deleted': 0, 'updated': 0, 'unchanged': 1, 'skipped_conflicts': 0}


def test_duplicate_group_shrinking_keeps_surviving_rows_correctly_labeled(conn):
    """Three shopee payouts share (date, amount): occurrence 1/2/3 with
    n_orders 5/7/3. The occurrence-1 payout (n=5) drops out of a later
    rebuild (real payouts renumber by id/time order on every rebuild, since
    marketplace_payouts is fully deleted+rebuilt) — the two survivors
    reflow to occurrence 1/2 with DIFFERENT n_orders than before. The
    surviving cashbook rows must end up describing what occurrence 1/2
    actually mean NOW, and the total row count/amount must match the two
    real payouts that remain — never a stale label surviving under a
    reused occurrence slot."""
    spx = _account_id(conn, 'SPX')
    _seed_payout(conn, 'shopee', '2026-03-10', 100.00, n_orders=5)
    _seed_payout(conn, 'shopee', '2026-03-10', 100.00, n_orders=7)
    _seed_payout(conn, 'shopee', '2026-03-10', 100.00, n_orders=3)
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    assert len(_payout_rows(conn, spx)) == 3

    # Rebuild: the n=5 payout is gone; n=7 and n=3 remain (id order preserved).
    conn.execute("DELETE FROM marketplace_payouts WHERE platform='shopee' AND n_orders=5")
    conn.commit()

    mirror.mirror_platform(conn, 'shopee')

    rows = _payout_rows(conn, spx)
    assert len(rows) == 2, "row count must match the 2 real payouts that remain"
    assert sum(float(r['amount']) for r in rows) == 200.00, "total ฿ must match 2 real payouts"
    descriptions = {r['description'] for r in rows}
    assert descriptions == {'Shopee โอนเงิน (7 ออเดอร์)', 'Shopee โอนเงิน (3 ออเดอร์)'}, (
        "every surviving row must describe a payout that genuinely still exists — "
        "no row may keep describing the removed n=5 payout")


# ── A mid-write failure must leave NOTHING committed (review finding #2):
# get_connection() sets no isolation_level, so a raised exception with no
# rollback leaves partial INSERT/UPDATE/DELETE statements pending on the
# connection — durably flushed by a LATER, unrelated commit on the same
# connection (e.g. a sibling platform's reconcile_payouts in the same
# /marketplace/upload request), silently contradicting "nothing written,
# nothing committed on failure". ─────────────────────────────────────────

class _RaisingConn:
    """Wraps a real sqlite3 connection; raises on the Nth INSERT so the
    seam under test (mid-loop failure) can be hit deterministically. Every
    other call (SELECT, UPDATE, DELETE, commit, rollback) forwards to the
    real connection so the real DB state can be asserted afterward."""
    def __init__(self, real_conn, fail_after_n_inserts):
        self._real = real_conn
        self._fail_after = fail_after_n_inserts
        self._insert_count = 0

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith('INSERT'):
            self._insert_count += 1
            if self._insert_count > self._fail_after:
                raise sqlite3.OperationalError('simulated mid-loop failure')
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_mid_write_failure_rolls_back_everything_this_call_wrote(conn):
    spx = _account_id(conn, 'SPX')
    for i in range(3):
        _seed_payout(conn, 'shopee', f'2026-03-{10+i:02d}', 100.00 + i, n_orders=1)
    conn.commit()

    wrapped = _RaisingConn(conn, fail_after_n_inserts=1)
    with pytest.raises(sqlite3.OperationalError):
        mirror.mirror_platform(wrapped, 'shopee')

    assert _payout_rows(conn, spx) == [], (
        "a mid-loop failure must roll back its own partial writes — a later, "
        "unrelated commit on this same connection must not silently flush them")


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


# ── Deploy-then-import ordering hazard (review finding): a manual row that
# already represents a real payout must never get a silent duplicate. This
# is exactly the scenario scripts/convert_legacy_cashbook_payout_rows.py
# exists to close BEFORE the mirror ever runs in prod — this guard is the
# safety net for if that ordering is ever violated (a fresh deploy, a
# missed step, a re-run before the conversion). ────────────────────────────

def test_manual_row_within_window_blocks_insert_no_double_booking(conn):
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
    assert result == {'inserted': 0, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 1}

    all_rows = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE account_id=? AND amount=3596.00",
        (spx,),
    ).fetchall()
    assert len(all_rows) == 1, "must not create a duplicate alongside the un-converted manual row"
    assert all_rows[0]['payout_platform'] is None
    assert all_rows[0]['description'] == 'คีย์มือ', "the manual row itself must still be untouched"


def test_manual_row_outside_window_does_not_block_insert(conn):
    """A genuinely unrelated manual row (same amount, but far outside the
    2-day match window) is not a plausible conversion candidate, so the
    payout is not a real conflict — the insert must proceed normally."""
    spx = _account_id(conn, 'SPX')
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-01-15', 'income', 'ยอดขายของ', 3596.00, 'ไม่เกี่ยวกัน', 'พุธ')",
        (spx,),
    )
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00)
    conn.commit()

    result = mirror.mirror_platform(conn, 'shopee')
    assert result == {'inserted': 1, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}

    mirrored = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE account_id=? AND payout_platform='shopee'",
        (spx,),
    ).fetchall()
    assert len(mirrored) == 1


def test_skipped_conflict_resolves_once_manual_row_is_converted(conn):
    """Once the manual row is converted (the one-time script's job) or
    simply deleted, the next mirror run inserts the payout it deferred."""
    spx = _account_id(conn, 'SPX')
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-03-10', 'income', 'ยอดขายของ', 3596.00, 'คีย์มือ', 'พุธ')",
        (spx,),
    )
    _seed_payout(conn, 'shopee', '2026-03-10', 3596.00, n_orders=9)
    conn.commit()

    result1 = mirror.mirror_platform(conn, 'shopee')
    assert result1['skipped_conflicts'] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payout_platform='shopee'"
    ).fetchone()[0] == 0

    conn.execute("DELETE FROM cashbook_transactions WHERE description='คีย์มือ'")
    conn.commit()

    result2 = mirror.mirror_platform(conn, 'shopee')
    assert result2 == {'inserted': 1, 'deleted': 0, 'updated': 0, 'unchanged': 0, 'skipped_conflicts': 0}
    rows = _payout_rows(conn, spx)
    assert len(rows) == 1 and rows[0]['description'] == 'Shopee โอนเงิน (9 ออเดอร์)'


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
