"""scripts/2026-09-30_redate_cashbook_642.py — Card F PR-0 (Q7 = B).

Runs against a fresh init_db() file seeded with the exact 642/26 shape, never
the live DB. Loaded by path (a name collision between inventory_app/ and
scripts/ makes plain import order-dependent, see test_convert_legacy_cashbook_payout_rows.py).
"""
import importlib.util as _ilu
import json
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import database

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts', '2026-09-30_redate_cashbook_642.py')
_spec = _ilu.spec_from_file_location('redate_cashbook_642_under_test', _SCRIPT)
redate = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(redate)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'inventory.db')
    database.init_db(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("INSERT INTO cashbook_accounts (id, code) VALUES (5, '392')")
    c.commit()
    c.close()
    _seed(path)
    return path


def _seed(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = OFF")  # payroll_runs/employees are not part of what is under test
    c.execute("INSERT INTO salary_advances (id, advance_date, amount, deducted_in_run_id)"
              " VALUES (26, '2026-06-30', 3000.0, 6)")
    c.execute("INSERT INTO cashbook_transactions (id, account_id, txn_date, direction, category, amount,"
              " salary_advance_id) VALUES (642, 5, '2026-06-01', 'expense', 'เบิกเงินเดือน', 3000.0, 26)")
    # a bystander in the same account-month, to make the SUM/COUNT invariant mean something
    c.execute("INSERT INTO cashbook_transactions (id, account_id, txn_date, direction, category, amount)"
              " VALUES (700, 5, '2026-06-15', 'expense', 'x', 125.5)")
    c.commit()
    c.close()


def _q(path, sql, *a):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def _snapshot(path):
    return (_q(path, "SELECT * FROM cashbook_transactions ORDER BY id"),
            _q(path, "SELECT * FROM salary_advances ORDER BY id"),
            _q(path, "SELECT COUNT(*) FROM audit_log"))


def _run(path, *extra):
    return redate.main(['--db', path, '--operator', 'Put', *extra])


def test_rehearse_writes_nothing(db, capsys):
    before = _snapshot(db)
    assert _run(db) == 0
    assert _snapshot(db) == before
    assert 'REHEARSE' in capsys.readouterr().out


def test_apply_changes_only_txn_date_and_audits(db):
    before = _q(db, "SELECT * FROM cashbook_transactions ORDER BY id")
    n_audit = _q(db, "SELECT COUNT(*) FROM audit_log")[0][0]
    assert _run(db, '--apply') == 0
    after = _q(db, "SELECT * FROM cashbook_transactions ORDER BY id")
    cols = [r[1] for r in _q(db, "PRAGMA table_info(cashbook_transactions)")]
    diffs = [(b[0], cols[i], b[i], a[i]) for b, a in zip(before, after) for i in range(len(cols)) if b[i] != a[i]]
    assert diffs == [(642, 'txn_date', '2026-06-01', '2026-06-30')]
    # trigger row + the explicit row
    rows = _q(db, "SELECT changed_fields, user FROM audit_log WHERE table_name='cashbook_transactions'"
                  " AND row_id=642 AND action='UPDATE' ORDER BY id")
    assert len(_q(db, "SELECT 1 FROM audit_log")) == n_audit + 2
    explicit = [r for r in rows if r[1] == 'Put']
    assert len(explicit) == 1
    assert json.loads(explicit[0][0]) == {'txn_date': ['2026-06-01', '2026-06-30'],
                                          'reason': 'Q7 B: match salary_advances 26'}


def test_second_apply_is_refused_and_writes_nothing(db, capsys):
    assert _run(db, '--apply') == 0
    snap = _snapshot(db)
    assert _run(db, '--apply') == 2
    assert 'txn_date' in capsys.readouterr().err
    assert _snapshot(db) == snap


@pytest.mark.parametrize('sql, needle', [
    ("DELETE FROM cashbook_transactions WHERE id=642", 'does not exist'),
    ("UPDATE cashbook_transactions SET salary_advance_id=NULL WHERE id=642", 'salary_advance_id'),
    ("UPDATE cashbook_transactions SET txn_date='2026-06-02' WHERE id=642", 'txn_date'),
    ("UPDATE cashbook_transactions SET amount=3001 WHERE id=642", 'amount is'),
    ("DELETE FROM salary_advances WHERE id=26", None),
    ("UPDATE salary_advances SET advance_date='2026-06-29' WHERE id=26", 'advance_date'),
    ("UPDATE salary_advances SET amount=2999 WHERE id=26", 'amount is'),
    ("UPDATE salary_advances SET deducted_in_run_id=7 WHERE id=26", 'deducted_in_run_id'),
    ("INSERT INTO cashbook_transactions (id, account_id, txn_date, direction, amount, salary_advance_id)"
     " VALUES (701, 5, '2026-06-30', 'expense', 3000.0, 26)", 'exactly 1'),
])
def test_each_precondition_refuses(db, sql, needle, capsys):
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys = OFF")
    if 'VALUES (701' in sql:  # a unique index normally makes a second link impossible; drop it to test the check
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name="
                                 "'cashbook_transactions' AND sql LIKE '%UNIQUE%salary_advance_id%'").fetchall():
            c.execute(f'DROP INDEX "{name}"')
    c.execute(sql)
    c.commit()
    c.close()
    snap = _snapshot(db)
    for extra in ((), ('--apply',)):
        assert _run(db, *extra) == 2
    err = capsys.readouterr().err
    assert 'REFUSED' in err
    if needle:
        assert needle in err
    assert _snapshot(db) == snap


def test_june_sum_invariant_failure_rolls_back(db, monkeypatch):
    calls = iter([(3125.5, 2), (9.0, 2)])
    monkeypatch.setattr(redate, '_month_totals', lambda *a: next(calls))
    snap = _snapshot(db)
    assert _run(db, '--apply') == 3
    assert _snapshot(db) == snap


def test_apply_rereads_on_a_fresh_connection(db, monkeypatch):
    real = sqlite3.connect
    opened = []

    def counting(*a, **k):
        opened.append(1)
        return real(*a, **k)
    monkeypatch.setattr(redate.sqlite3, 'connect', counting)
    assert _run(db, '--apply') == 0
    assert len(opened) == 2  # the write connection, then a separate one for the re-read
