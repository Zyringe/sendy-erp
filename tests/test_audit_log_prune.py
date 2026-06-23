"""TTL prune for audit_log — transactions-only churn retention (option B).

audit_log is the largest table in the DB (one big historical BSN import churned
~390k rows in a single day, ~half the file: roughly half INSERT, half DELETE —
the import rebuild deletes-then-reinserts ledger rows). `transactions` rows are
~95% of the table. Organic growth is ~2,400 rows/day.

models.prune_audit_log() is the recurring guard, hooked into the import-confirm
flow so the table self-limits to AUDIT_LOG_RETENTION_DAYS without manual ops.
The predicate prunes ONLY old `transactions` INSERT+DELETE churn (the import
rebuild). Every other table's audit — and all UPDATEs/DELETEs everywhere,
including every finance-table INSERT (a created payout / receipt / invoice) —
is kept FOREVER. That kills the bulk (~95% of rows) while preserving the entire
money-table and human-edit forensic trail.

These tests lock that intent: old `transactions` churn is pruned, everything
else (including old non-`transactions` INSERTs on finance tables) survives, the
boundary day is kept, and the function is idempotent.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from datetime import date, timedelta

import models


def _seed(db_path, rows):
    """Wipe audit_log on the temp copy and insert (table_name, action, days_ago)
    rows. `days_ago` is relative to today; the cutoff is AUDIT_LOG_RETENTION_DAYS
    ago. created_at carries a real time component (' 09:00:00') to match prod —
    so the date-only-vs-datetime boundary string comparison is exercised, not a
    bare date. Returns a list of (id, table_name, action, days_ago) in order."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM audit_log")
        out = []
        for table_name, action, days_ago in rows:
            created = (date.today() - timedelta(days=days_ago)).isoformat() + ' 09:00:00'
            cur = conn.execute(
                "INSERT INTO audit_log (table_name, row_id, action, changed_fields, created_at) "
                "VALUES (?, 1, ?, '{}', ?)",
                (table_name, action, created),
            )
            out.append((cur.lastrowid, table_name, action, days_ago))
        conn.commit()
        return out
    finally:
        conn.close()


def _ids_present(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM audit_log").fetchall()}
    finally:
        conn.close()


def test_retention_constant_is_90_days():
    """The retention window is a single named constant (the policy lever)."""
    assert models.AUDIT_LOG_RETENTION_DAYS == 90


def test_action_aware_keep_and_prune(tmp_db):
    """The core predicate contract, all in one table of cases.

    R = retention days. `R + 10` is comfortably OLD; `R - 10` is inside window.
    Only old `transactions` INSERT+DELETE is pruned; everything else is kept.
    """
    R = models.AUDIT_LOG_RETENTION_DAYS
    OLD, NEW = R + 10, R - 10

    seeded = _seed(tmp_db, [
        # forensic trail / non-transactions — KEEP forever, even when old
        ('products',          'UPDATE', OLD),   # price/cost edit
        ('received_payments', 'DELETE', OLD),   # a hand-void of a money row
        ('commission_payouts','DELETE', OLD),   # payout void
        ('commission_payouts','INSERT', OLD),   # payout CREATION — option B keeps this
        ('paid_invoices',     'INSERT', OLD),   # invoice creation — kept
        ('products',          'INSERT', OLD),   # any non-transactions INSERT — kept
        ('transactions',      'UPDATE', OLD),   # ledger row edit (real change) — kept
        # low-value churn — PRUNE when old (transactions INSERT + DELETE only)
        ('transactions',      'INSERT', OLD),   # import insert churn
        ('transactions',      'DELETE', OLD),   # import delete-then-reinsert churn
        # within window — KEEP regardless of action (boundary protection)
        ('transactions',      'INSERT', NEW),
        ('transactions',      'DELETE', NEW),
    ])
    by_label = {(t, a, d): i for (i, t, a, d) in seeded}

    n = models.prune_audit_log()
    present = _ids_present(tmp_db)

    expect_keep = [
        ('products', 'UPDATE', OLD),
        ('received_payments', 'DELETE', OLD),
        ('commission_payouts', 'DELETE', OLD),
        ('commission_payouts', 'INSERT', OLD),
        ('paid_invoices', 'INSERT', OLD),
        ('products', 'INSERT', OLD),
        ('transactions', 'UPDATE', OLD),
        ('transactions', 'INSERT', NEW),
        ('transactions', 'DELETE', NEW),
    ]
    expect_prune = [
        ('transactions', 'INSERT', OLD),
        ('transactions', 'DELETE', OLD),
    ]

    for key in expect_keep:
        assert by_label[key] in present, f"should KEEP {key}"
    for key in expect_prune:
        assert by_label[key] not in present, f"should PRUNE {key}"
    assert n == len(expect_prune), "reported delete count must match pruned set"


def test_old_products_update_is_kept(tmp_db):
    """An old price/cost edit (products UPDATE) is forensic — never pruned."""
    seeded = _seed(tmp_db, [('products', 'UPDATE', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    models.prune_audit_log()
    assert seeded[0][0] in _ids_present(tmp_db)


def test_old_received_payments_delete_is_kept(tmp_db):
    """An old received_payments DELETE (a void) is forensic — never pruned."""
    seeded = _seed(tmp_db, [('received_payments', 'DELETE', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    models.prune_audit_log()
    assert seeded[0][0] in _ids_present(tmp_db)


def test_old_commission_payouts_insert_is_kept(tmp_db):
    """An old commission_payouts INSERT (a payout CREATION on a money table) is
    kept forever under option B — it would have been pruned by the old
    all-table-INSERT predicate. This locks the money-tables-forever choice."""
    seeded = _seed(tmp_db, [('commission_payouts', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    n = models.prune_audit_log()
    assert n == 0
    assert seeded[0][0] in _ids_present(tmp_db)


def test_old_nontxn_insert_is_kept(tmp_db):
    """Any non-`transactions` INSERT (e.g. products) is kept forever — option B
    only prunes `transactions` churn, not all-table INSERTs."""
    seeded = _seed(tmp_db, [('products', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    n = models.prune_audit_log()
    assert n == 0
    assert seeded[0][0] in _ids_present(tmp_db)


def test_old_transactions_insert_is_pruned(tmp_db):
    """An old transactions INSERT (import churn) is pruned."""
    seeded = _seed(tmp_db, [('transactions', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    n = models.prune_audit_log()
    assert n == 1
    assert seeded[0][0] not in _ids_present(tmp_db)


def test_old_transactions_delete_is_pruned(tmp_db):
    """An old transactions DELETE (delete-then-reinsert import churn) is pruned."""
    seeded = _seed(tmp_db, [('transactions', 'DELETE', models.AUDIT_LOG_RETENTION_DAYS + 30)])
    n = models.prune_audit_log()
    assert n == 1
    assert seeded[0][0] not in _ids_present(tmp_db)


def test_within_window_transactions_insert_is_kept(tmp_db):
    """A transactions INSERT inside the window survives (boundary protection)."""
    seeded = _seed(tmp_db, [('transactions', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS - 1)])
    models.prune_audit_log()
    assert seeded[0][0] in _ids_present(tmp_db)


def test_boundary_day_is_kept(tmp_db):
    """A `transactions` churn row dated exactly at the cutoff (retention days ago,
    09:00:00) is kept.

    The cutoff is date-only (`date('now',...)`) while created_at carries a time
    component, so `'<cutoff-date> 09:00:00' < '<cutoff-date>'` is FALSE (the time
    suffix sorts AFTER the bare date string) → the boundary day is retained. An
    off-by-one (or a 'fix' to datetime()) would silently drop a day of churn at
    the boundary; this case exercises that text-comparison semantics."""
    seeded = _seed(tmp_db, [('transactions', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS)])
    models.prune_audit_log()
    assert seeded[0][0] in _ids_present(tmp_db)


def test_prune_is_idempotent(tmp_db):
    _seed(tmp_db, [
        ('transactions', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS + 5),
        ('transactions', 'DELETE', models.AUDIT_LOG_RETENTION_DAYS + 5),
    ])
    first = models.prune_audit_log()
    assert first == 2
    second = models.prune_audit_log()
    assert second == 0, "a second prune with no new prunable rows deletes nothing"


def test_prune_noop_when_no_old_churn(tmp_db):
    """No prunable old row → prune is a true no-op (returns 0), forensic kept."""
    _seed(tmp_db, [
        ('products', 'UPDATE', models.AUDIT_LOG_RETENTION_DAYS + 100),     # forensic, kept
        ('commission_payouts', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS + 100),  # money INSERT, kept
        ('transactions', 'INSERT', models.AUDIT_LOG_RETENTION_DAYS - 1),   # within window, kept
    ])
    assert models.prune_audit_log() == 0
    assert len(_ids_present(tmp_db)) == 3
