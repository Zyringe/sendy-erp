"""Phase 3 — commission payout auto-post + void symmetry (plan.md decisions
C1-C4/C7, scrutiny finding #2/#6). MONEY PATH.

`commission.record_payout(..., account_id=...)` / `commission.delete_payout`
post/void ONE linked `cashbook_transactions` expense row per
`commission_payouts.id`, mirroring `hr.post_salary_payment` /
`hr.void_salary_payment` (ADR 0006) and the Phase 2 advance write-back.

Fixture: `tmp_db_conn` (raw connection on a copy of the live DB) + mig 129
applied via `database.init_db()`.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database
import commission as commission_mod


@pytest.fixture
def migrated_conn(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _account_id(conn, code):
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    assert row is not None, f"expected seeded cashbook_accounts row code={code}"
    return row["id"]


def _transfer_account_id(conn):
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer=1 AND is_active=1 LIMIT 1"
    ).fetchone()
    assert row is not None, "expected a seeded is_transfer=1 active account (e.g. code 904)"
    return row["id"]


# ── 1. auto-post: exactly one linked cashbook expense, fields correct ──────

def test_record_payout_posts_one_linked_cashbook_expense(migrated_conn):
    conn = migrated_conn
    account_id = _account_id(conn, '392')

    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code='06', amount_paid=1500.5,
        paid_date='2026-07-05', paid_method='cash', note='ทดสอบ',
        paid_by='test-admin', account_id=account_id, conn=conn,
    )
    assert isinstance(payout_id, int)

    rows = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row['amount'] == 1500.5
    assert row['account_id'] == account_id
    assert row['txn_date'] == '2026-07-05'
    assert row['category'] == 'จ่ายค่าคอมมิชชั่น'
    assert row['direction'] == 'expense'
    assert row['created_by'] == 'test-admin'
    assert row['user_category'] == 'ต๋อ /06'
    assert 'ต๋อ /06' in (row['description'] or '')
    assert '2026-07' in (row['description'] or '')

    payout = conn.execute(
        "SELECT * FROM commission_payouts WHERE id=?", (payout_id,)
    ).fetchone()
    assert payout['salesperson_code'] == '06'
    assert payout['amount_paid'] == 1500.5


def test_record_payout_writes_actor_attributed_audit_row(migrated_conn):
    conn = migrated_conn
    account_id = _account_id(conn, '392')

    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code='03', amount_paid=500,
        paid_date='2026-07-05', paid_by='mgr-mom', account_id=account_id, conn=conn,
    )
    txn_id = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()['id']

    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE table_name='cashbook_transactions' "
        "AND row_id=? AND action='INSERT' AND user='mgr-mom'", (txn_id,)
    ).fetchone()
    assert row is not None, "auto-post must write an actor-attributed audit_log INSERT row"


# ── 2. atomicity: bad account -> ValueError, NOTHING written ───────────────

def test_record_payout_missing_account_rejected_nothing_written(migrated_conn):
    conn = migrated_conn
    n_payouts_before = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]

    with pytest.raises(ValueError):
        commission_mod.record_payout(
            year_month='2026-07', salesperson_code='06', amount_paid=999,
            paid_date='2026-07-06', account_id=999999, conn=conn,
        )

    n_payouts_after = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]
    assert n_payouts_after == n_payouts_before, "no commission_payouts row on a bad account"
    n_cb = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE amount=999"
    ).fetchone()[0]
    assert n_cb == 0


def test_record_payout_into_transfer_account_rejected(migrated_conn):
    conn = migrated_conn
    transfer_id = _transfer_account_id(conn)
    n_before = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]

    with pytest.raises(ValueError):
        commission_mod.record_payout(
            year_month='2026-07', salesperson_code='06', amount_paid=888,
            paid_date='2026-07-06', account_id=transfer_id, conn=conn,
        )

    n_after = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]
    assert n_after == n_before


def test_record_payout_into_inactive_account_rejected(migrated_conn):
    conn = migrated_conn
    account_id = _account_id(conn, '392')
    conn.execute("UPDATE cashbook_accounts SET is_active=0 WHERE id=?", (account_id,))
    conn.commit()
    n_before = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]

    with pytest.raises(ValueError):
        commission_mod.record_payout(
            year_month='2026-07', salesperson_code='06', amount_paid=777,
            paid_date='2026-07-06', account_id=account_id, conn=conn,
        )

    n_after = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]
    assert n_after == n_before


# ── 3. account_id=None (system top-up backfill) skips the cashbook side ───

def test_record_payout_without_account_id_skips_cashbook_post(migrated_conn):
    """Backward-compat for models.py::_topup_pre_feb_for_product — a
    retroactive correction, not a new cash movement (see record_payout's
    docstring). Must still insert the payout row, same as before this phase."""
    conn = migrated_conn
    n_cb_before = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]

    payout_id = commission_mod.record_payout(
        year_month='2026-02', salesperson_code='06', amount_paid=250,
        paid_date='2026-02-01', paid_method='auto', paid_by='system',
        note='pre-Feb 2026 auto-paid (top-up after brand change)', conn=conn,
    )
    assert isinstance(payout_id, int)

    n_cb_after = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]
    assert n_cb_after == n_cb_before, "no cashbook row when account_id is not given"
    linked = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()[0]
    assert linked == 0


# ── 4. void symmetry ────────────────────────────────────────────────────────

def test_delete_payout_removes_payout_and_linked_cashbook_row(migrated_conn):
    conn = migrated_conn
    account_id = _account_id(conn, '392')
    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code='06', amount_paid=333,
        paid_date='2026-07-07', account_id=account_id, paid_by='test-admin', conn=conn,
    )
    txn_id = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()['id']

    commission_mod.delete_payout(payout_id, actor='test-admin', conn=conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM commission_payouts WHERE id=?", (payout_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()[0] == 0

    audit = conn.execute(
        "SELECT 1 FROM audit_log WHERE table_name='cashbook_transactions' "
        "AND row_id=? AND action='DELETE' AND user='test-admin'", (txn_id,)
    ).fetchone()
    assert audit is not None, "void must write an actor-attributed audit_log DELETE row"


def test_delete_payout_on_historical_row_with_no_linked_cashbook_is_noop(migrated_conn):
    """A pre-Phase-3 payout (or an account_id=None top-up) has no linked
    cashbook row — deleting it must not raise and must still remove the
    payout row."""
    conn = migrated_conn
    payout_id = conn.execute(
        "INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid, paid_date)"
        " VALUES ('2026-01', '06', 100, '2026-01-15')"
    ).lastrowid
    conn.commit()

    commission_mod.delete_payout(payout_id, actor='test-admin', conn=conn)  # must not raise

    assert conn.execute(
        "SELECT COUNT(*) FROM commission_payouts WHERE id=?", (payout_id,)
    ).fetchone()[0] == 0


def test_record_then_delete_leaves_cashbook_clean(migrated_conn):
    conn = migrated_conn
    account_id = _account_id(conn, '392')
    n_cb_before = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]

    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code='06', amount_paid=444,
        paid_date='2026-07-08', account_id=account_id, conn=conn,
    )
    commission_mod.delete_payout(payout_id, actor='test-admin', conn=conn)

    n_cb_after = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]
    assert n_cb_after == n_cb_before


# ── 5. cashbook lock: a commission-linked row is READ-ONLY in the cashbook ─

def _client_as(role, tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def test_commission_linked_row_is_readonly_in_cashbook(migrated_conn, tmp_db):
    conn = migrated_conn
    account_id = _account_id(conn, '392')
    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code='06', amount_paid=600,
        paid_date='2026-07-09', account_id=account_id, paid_by='test-admin', conn=conn,
    )
    txn_id = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()['id']
    conn.close()  # the route opens its own connection on tmp_db

    c = _client_as('admin', tmp_db)
    resp_edit = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-07-10",
        "direction": "expense", "category": "จ่ายค่าคอมมิชชั่น", "amount": "999999",
    })
    assert resp_edit.status_code == 403

    resp_delete = c.post(f"/cashbook/txn/{txn_id}/delete", data={})
    assert resp_delete.status_code == 403

    conn2 = sqlite3.connect(tmp_db)
    row = conn2.execute(
        "SELECT amount, txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    conn2.close()
    assert row[0] == 600 and row[1] == '2026-07-09', \
        "commission-linked row must be unchanged by the cashbook edit/delete guard"
