"""scripts/2026_09_19_589_commission_03_backrecord.py (#589) on a prod-shaped clone.

The clone must hold the prod rows the script is written against (cashbook
345/331/639/845 and 03's four May-Sep invoices); on a DB without them the tests
skip. Everything is checked with plain SQL, not with the script's own helpers.
"""
import importlib.util
import os
import pathlib
import sqlite3

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts' / '2026_09_19_589_commission_03_backrecord.py'
MIG = REPO / 'data' / 'migrations' / '189_commission_03_tier_a.sql'


def _load():
    spec = importlib.util.spec_from_file_location('backrecord_589', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(tmp_db):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT account_id, category, user_category, amount, description "
                       "FROM cashbook_transactions WHERE id = 845").fetchone()
    if row != (1, 'อื่นๆ', 'ทวีเกียรติ', 400.0, 'คอมมิชชั่น'):
        conn.close()
        pytest.skip('clone does not hold prod cashbook row 845 as of 2026-09-19')
    conn.executescript(MIG.read_text(encoding='utf-8'))
    conn.close()
    return tmp_db


def _state(path):
    conn = sqlite3.connect(path)
    out = {
        'payouts': conn.execute("SELECT year_month, invoice_no, amount_paid FROM commission_payouts "
                                "WHERE salesperson_code = '03' ORDER BY year_month").fetchall(),
        'row_845': conn.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE id = 845").fetchone()[0],
        'linked': conn.execute(
            "SELECT c.account_id, c.txn_date, c.category, c.amount, p.invoice_no "
            "FROM cashbook_transactions c JOIN commission_payouts p ON p.id = c.commission_payout_id "
            "WHERE p.salesperson_code = '03'").fetchall(),
        'balance': conn.execute(
            "SELECT printf('%.2f', SUM(CASE WHEN direction='income' THEN amount ELSE -amount END)) "
            "FROM cashbook_transactions WHERE account_id = 1").fetchone()[0],
        'rows': conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0],
    }
    conn.close()
    return out


def test_rehearse_moves_845_and_back_records_the_hand_paid_months(db):
    before = _state(db)
    assert before['payouts'] == [] and before['row_845'] == 1

    assert _load().main(['rehearse', '--db', db]) == 0

    after = _state(db)
    assert after['payouts'] == [('2026-05', 'IV6900744', 85.65), ('2026-06', 'IV6900441', 874.8),
                                ('2026-07', 'IV6900531', 117.0), ('2026-09', 'IV6901059', 400.0)]
    assert after['row_845'] == 0
    assert after['linked'] == [(1, '2026-09-04', 'จ่ายค่าคอมมิชชั่น', 400.0, 'IV6901059')], \
        'only September posts a cashbook row; the hand-paid months are already in the book'
    assert after['balance'] == before['balance'], 'no baht moved in account 1'
    assert after['rows'] == before['rows']


def test_second_run_refuses_and_changes_nothing(db):
    mod = _load()
    assert mod.main(['rehearse', '--db', db]) == 0
    once = _state(db)
    assert mod.main(['rehearse', '--db', db]) == 2
    assert _state(db) == once


def test_a_failed_invariant_rolls_everything_back(db):
    mod = _load()
    mod.EXPECTED_REMAINING = dict(mod.EXPECTED_REMAINING, IV6901059=-149.0)
    before = _state(db)
    assert mod.main(['rehearse', '--db', db]) == 1
    assert _state(db) == before


def test_rehearse_refuses_the_prod_path(db):
    mod = _load()
    mod.PROD_DB = os.path.realpath(db)
    before = _state(db)
    assert mod.main(['rehearse', '--db', db]) == 2
    assert _state(db) == before


def test_live_needs_the_confirm_string(db):
    mod = _load()
    before = _state(db)
    assert mod.main(['live', '--db', db]) == 2
    assert mod.main(['live', '--db', db, '--confirm', 'yes']) == 2
    assert _state(db) == before
