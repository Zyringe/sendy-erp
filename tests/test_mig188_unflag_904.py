"""Migration 188 — account 904 loses is_transfer, keeps is_active = 0 (#594, ADR 0017).

The one state it must never produce is 904 ACTIVE with is_transfer = 0: that
is an ordinary pay-from account, and every guard that keeps 904 out of a
salary or commission posting would then say yes. So the flip is CONDITIONAL
on 904 being inactive, and on any other state the migration is a stamped
no-op. It never aborts: an abort crashes boot (the runner re-raises out of
`import app`, and under gunicorn --preload the master exits), which would hit
every stale local DB where 904 is still active. A 904 re-activated on prod
before the deploy is caught by the post-deploy check instead (PR #617).

Fixture: `empty_db` (full live schema, zero rows), with 904 inserted in the
exact state each case needs — never `tmp_db`, whose 904 is whatever the live
seed happens to hold.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '188_unflag_904_is_transfer.sql'
_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'data', 'migrations')
FORWARD = os.path.join(_DIR, MIG)
ROLLBACK = os.path.join(_DIR, '188_unflag_904_is_transfer.rollback.sql')


def _conn(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(db, is_transfer, is_active, with_904=True):
    conn = _conn(db)
    conn.execute("INSERT INTO cashbook_accounts (code, is_active, is_transfer) VALUES ('392', 1, 0)")
    if with_904:
        conn.execute(
            """INSERT INTO cashbook_accounts
                 (code, display_name, note, is_active, is_transfer, sort_order,
                  created_at, updated_at)
               VALUES ('904', NULL, 'ประวัติก่อนเริ่มใช้สมุด', ?, ?, 100,
                       '2026-05-18 10:00:00', '2026-06-09 12:13:04')""",
            (is_active, is_transfer))
    conn.commit()
    conn.close()


def _rows(db):
    """Every column of every account, keyed by code: the byte-identity oracle."""
    conn = _conn(db)
    out = {r['code']: tuple(r) for r in conn.execute('SELECT * FROM cashbook_accounts ORDER BY id')}
    conn.close()
    return out


def _flags(db):
    conn = _conn(db)
    r = conn.execute("SELECT is_transfer, is_active FROM cashbook_accounts WHERE code='904'").fetchone()
    conn.close()
    return None if r is None else (r['is_transfer'], r['is_active'])


def _apply(db, path):
    """Exactly what database.run_pending_migrations does with the file."""
    conn = sqlite3.connect(db)
    try:
        with open(path, encoding='utf-8') as f:
            conn.executescript(f.read())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _stamped(db):
    conn = _conn(db)
    n = conn.execute('SELECT COUNT(*) FROM applied_migrations WHERE filename = ?', (MIG,)).fetchone()[0]
    conn.close()
    return n


# ── the flip ─────────────────────────────────────────────────────────────────

def test_flips_is_transfer_and_leaves_everything_else_alone(empty_db):
    _seed(empty_db, is_transfer=1, is_active=0)
    before = _rows(empty_db)
    _apply(empty_db, FORWARD)
    after = _rows(empty_db)
    assert _flags(empty_db) == (0, 0)
    # every other column of 904, updated_at included, and every other account
    cols = [r[1] for r in sqlite3.connect(empty_db).execute('PRAGMA table_info(cashbook_accounts)')]
    i = cols.index('is_transfer')
    assert after['904'][:i] + after['904'][i + 1:] == before['904'][:i] + before['904'][i + 1:]
    assert after['392'] == before['392']


def test_the_runner_applies_and_stamps_it(empty_db):
    """Through database.run_pending_migrations, with every OTHER file stamped
    so 188 is the only pending one (empty_db's applied_migrations is empty,
    which would otherwise take the bootstrap-backfill path and run nothing)."""
    _seed(empty_db, is_transfer=1, is_active=0)
    conn = sqlite3.connect(empty_db)
    others = [f for f in database._list_migration_files() if f != MIG]
    assert MIG in database._list_migration_files()
    conn.executemany("INSERT INTO applied_migrations (filename, applied_by) VALUES (?, 'test')",
                     [(f,) for f in others])
    conn.commit()
    ran = database.run_pending_migrations(conn, verbose=False)
    conn.close()
    assert ran == [MIG]
    assert _flags(empty_db) == (0, 0)
    assert _stamped(empty_db) == 1


def test_rerun_on_an_already_unflagged_904_is_a_no_op(empty_db):
    _seed(empty_db, is_transfer=0, is_active=0)
    before = _rows(empty_db)
    _apply(empty_db, FORWARD)
    assert _rows(empty_db) == before


# ── the condition: an active or absent 904 is a stamped no-op ──────────────

def _stamp_others(db):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO applied_migrations (filename, applied_by) VALUES (?, 'test')",
                     [(f,) for f in database._list_migration_files() if f != MIG])
    conn.commit()
    conn.close()


def test_an_active_904_is_left_alone_and_boot_goes_on(empty_db):
    """The shared local dev DB's shape (904 still active, still a transfer
    account): the runner applies 188, stamps it, and changes nothing."""
    _seed(empty_db, is_transfer=1, is_active=1)
    before = _rows(empty_db)
    _stamp_others(empty_db)
    conn = sqlite3.connect(empty_db)
    ran = database.run_pending_migrations(conn, verbose=False)
    conn.close()
    assert ran == [MIG]
    assert _stamped(empty_db) == 1
    assert _rows(empty_db) == before


def test_an_active_904_opens_no_pay_path(empty_db):
    """After 188 runs on an active 904, 904 is still refused by the pay-from
    picker and by the commission write path — because it is still a
    transfer account. CONTROL: 392, active and not a transfer account, is
    offered and accepted, so the refusal is about 904, not the setup."""
    import commission
    import hr_queries as hrq
    _seed(empty_db, is_transfer=1, is_active=1)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('T594', 'ทดสอบ')")
    conn.commit()
    conn.close()
    _apply(empty_db, FORWARD)
    conn = _conn(empty_db)
    ids = {r['code']: r['id'] for r in conn.execute('SELECT id, code FROM cashbook_accounts')}
    offered = {r['code'] for r in hrq.get_active_cashbook_accounts(conn, non_transfer_only=True)}
    assert offered == {'392'}
    commission.record_payout(year_month='2026-09', salesperson_code='T594', amount_paid=1.0,
                             paid_date='2026-09-19', account_id=ids['392'], conn=conn)
    with pytest.raises(ValueError):
        commission.record_payout(year_month='2026-09', salesperson_code='T594', amount_paid=1.0,
                                 paid_date='2026-09-19', account_id=ids['904'], conn=conn)
    conn.close()


def test_a_db_without_904_is_a_stamped_no_op(empty_db):
    _seed(empty_db, is_transfer=0, is_active=1, with_904=False)
    before = _rows(empty_db)
    assert '904' not in before and '392' in before   # CONTROL: the seed ran
    _stamp_others(empty_db)
    conn = sqlite3.connect(empty_db)
    assert database.run_pending_migrations(conn, verbose=False) == [MIG]
    conn.close()
    assert _rows(empty_db) == before
    assert _stamped(empty_db) == 1


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_restores_the_row_byte_identically(empty_db):
    _seed(empty_db, is_transfer=1, is_active=0)
    before = _rows(empty_db)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO applied_migrations (filename, applied_by) VALUES (?, 'test')", (MIG,))
    conn.commit()
    conn.close()
    _apply(empty_db, FORWARD)
    assert _rows(empty_db) != before                  # CONTROL: forward changed something
    _apply(empty_db, ROLLBACK)
    assert _rows(empty_db) == before
    assert _stamped(empty_db) == 0


def test_forward_after_rollback_lands_the_same_state(empty_db):
    _seed(empty_db, is_transfer=1, is_active=0)
    _apply(empty_db, FORWARD)
    once = _rows(empty_db)
    _apply(empty_db, ROLLBACK)
    _apply(empty_db, FORWARD)
    assert _rows(empty_db) == once


def test_rollback_after_a_no_op_forward_changes_nothing(empty_db):
    _seed(empty_db, is_transfer=1, is_active=1)
    before = _rows(empty_db)
    _apply(empty_db, FORWARD)
    _apply(empty_db, ROLLBACK)
    assert _rows(empty_db) == before
