"""Card F oracle break-once (plan §5 PR-1): on the scenario DB, seed ONE
violating row per invariant and assert the oracle names that row under that
invariant, and nothing new anywhere else except the co-violations a row of that
shape cannot avoid (declared per case).

The clean scenario must read all-zero first: that is the control that makes
each red below mean "this predicate caught it".
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import ast
import importlib.util
import json
import sqlite3
import subprocess
import sys

import pytest

import database
from tests import cashbook_seam_scenario as scn

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SCRIPT = os.path.join(REPO, 'scripts', 'audit_cashbook_invariants.py')


def _load():
    spec = importlib.util.spec_from_file_location('audit_cashbook_invariants', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oracle = _load()


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    """build_db + seed + the shopee payouts mirrored, so I6 starts clean."""
    import cashbook_payout_mirror as mirror
    path = str(tmp_path_factory.mktemp('oracle') / 'template.db')
    scn.build_db(path)
    c = database._connect(path)
    ids = scn.seed(c)
    assert mirror.mirror_platform(c, 'shopee')['inserted'] == 2
    c.close()
    return path, ids


@pytest.fixture
def db(template, tmp_path):
    src, ids = template
    dst = str(tmp_path / 'o.db')
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    s.close()
    # A fresh .backup of a WAL database cannot be opened mode=ro until a
    # read-write connection has opened it once (verification-testing rules).
    d.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()
    d.close()
    return dst, ids


def _run(path):
    c = oracle.connect_ro(path)
    try:
        return {r['id']: r for r in oracle.audit(c)}
    finally:
        c.close()


def _exec(path, sql, params=()):
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = OFF")
    cur = c.execute(sql, params)
    c.commit()
    c.close()
    return cur.lastrowid


def _txn(path, ids, **cols):
    vals = dict(account_id=ids['acct_392'], txn_date='2026-08-20', direction='expense',
                category=scn.MANUAL_CATEGORY, amount=100.0, created_by='t')
    vals.update(cols)
    return _exec(path, f"INSERT INTO cashbook_transactions ({', '.join(vals)}) VALUES "
                       f"({', '.join('?' * len(vals))})", tuple(vals.values()))


def _payout(path, amount=500.0, paid_date='2026-09-02', invoice='IV1', ym='2026-08', sp='06'):
    return _exec(path, "INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid, "
                       "paid_date, invoice_no) VALUES (?, ?, ?, ?, ?)",
                 (ym, sp, amount, paid_date, invoice))


def test_clean_scenario_reads_all_zero(db):
    """The control: every invariant ran over real rows and found nothing."""
    path, _ = db
    res = _run(path)
    assert set(res) == {k for k, _, _ in oracle.INVARIANTS}
    assert all(r['violators'] == [] for r in res.values()), {k: r['violators'] for k, r in res.items()}
    # non-vacuity: the populations the checks read are not empty
    assert res['I1']['value'].startswith('3 rows'), res['I1']['value']
    assert res['I6']['value'] == 'lazada 0 = 0, shopee 2 = 2'
    assert res['I7']['value'] == '0 / 0'


def _case_i2(p, ids):
    pid = _payout(p, amount=15000.0, paid_date='2026-09-01')
    return _txn(p, ids, payroll_item_id=ids['item1'], payroll_run_id=ids['run'],
                commission_payout_id=pid, category=scn.SALARY_CATEGORY, amount=15000.0,
                txn_date='2026-09-01'), 'txn', {'I12'}


def _case_i3(p, ids):
    _exec(p, "UPDATE salary_advances SET employee_id = 9999 WHERE id = ?", (ids['advance'],))
    return ids['advance'], 'salary_advances', set()


def _case_i4(p, ids):
    return _exec(p, "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id) "
                    "VALUES (?, '2026-09-01', 50.0, ?)", (ids['emp1'], ids['acct_392'])), 'advance', set()


def _case_i5(p, ids):
    run2 = _exec(p, "INSERT INTO payroll_runs (year_month, status) VALUES ('2026-09', 'draft')")
    item3 = _exec(p, "INSERT INTO payroll_items (run_id, employee_id, net_pay) VALUES (?, ?, 900)",
                  (run2, ids['emp1']))
    return _txn(p, ids, payroll_item_id=item3, payroll_run_id=run2, category=scn.SALARY_CATEGORY,
                amount=900.0), 'txn', set()


def _case_i6(p, ids):
    _exec(p, "DELETE FROM cashbook_transactions WHERE id = (SELECT MAX(id) FROM "
             "cashbook_transactions WHERE payout_platform = 'shopee')")
    return 'shopee:missing:2026-08-15/1500.0/2', None, set()


def _case_i6b(p, ids):
    return _exec(p, "INSERT INTO marketplace_payouts (platform, deposit_date, amount) "
                    "VALUES ('lazada', '2025-12-01', -1.0)"), 'marketplace_payout', set()


def _case_i7(p, ids):
    return _txn(p, ids, commission_payout_id=9999, category=scn.COMMISSION_CATEGORY), 'txn', {'I3'}


def _case_i8_date(p, ids):
    return _txn(p, ids, txn_date='31/08/2569'), 'txn', set()


def _case_i8_amount(p, ids):
    return _txn(p, ids, amount=float('inf')), 'txn', set()


def _case_i9(p, ids):
    return _txn(p, ids, category='หมวดที่ไม่มี'), 'txn', set()


def _case_i10(p, ids):
    return _txn(p, ids, payroll_item_id=ids['item1'], payroll_run_id=ids['run'],
                category=scn.SALARY_CATEGORY, amount=14999.0), 'txn', set()


def _case_i11(p, ids):
    _exec(p, "UPDATE cashbook_transactions SET txn_date = '2026-08-01' WHERE id = ?",
          (ids['advance_txn'],))
    return ids['advance_txn'], 'txn', set()


def _case_i12(p, ids):
    pid = _payout(p)
    return _txn(p, ids, commission_payout_id=pid, category=scn.COMMISSION_CATEGORY,
                amount=499.0, txn_date='2026-09-02'), 'txn', set()


def _case_i13(p, ids):
    c = sqlite3.connect(p)
    txn = c.execute("SELECT MIN(id) FROM cashbook_transactions WHERE payout_platform='shopee'"
                    ).fetchone()[0]
    c.close()
    _exec(p, "UPDATE cashbook_transactions SET account_id = ? WHERE id = ?", (ids['acct_392'], txn))
    return txn, 'txn', {'I6'}


def _case_i14_advance(p, ids):
    return _txn(p, ids, category=scn.ADVANCE_CATEGORY), 'txn', set()


def _case_i14_salary(p, ids):
    return _txn(p, ids, category=scn.SALARY_CATEGORY), 'txn', set()


def _case_i15(invoice):
    def case(p, ids):
        a, b = _payout(p, invoice=invoice), _payout(p, invoice=invoice)
        for pid in (a, b):
            _txn(p, ids, commission_payout_id=pid, category=scn.COMMISSION_CATEGORY,
                 amount=500.0, txn_date='2026-09-02')
        return [a, b], 'commission_payout', set()
    return case


CASES = {
    'I2': _case_i2, 'I3': _case_i3, 'I4': _case_i4, 'I5': _case_i5, 'I6': _case_i6,
    'I6b': _case_i6b, 'I7': _case_i7, 'I8/date': _case_i8_date, 'I8/amount': _case_i8_amount,
    'I9': _case_i9, 'I10': _case_i10, 'I11': _case_i11, 'I12': _case_i12, 'I13': _case_i13,
    'I14/advance': _case_i14_advance, 'I14/salary': _case_i14_salary,
    'I15/mode1': _case_i15('IV1'), 'I15/mode2': _case_i15(None),
}


@pytest.mark.parametrize('case', sorted(CASES))
def test_oracle_names_the_seeded_violator(db, case):
    path, ids = db
    inv = case.split('/')[0]
    got, prefix, also = CASES[case](path, ids)
    if prefix is None:
        want = [got]
    elif prefix == 'salary_advances':
        want = [v for v in _run(path)['I3']['violators'] if v.startswith(f'salary_advances:{got}->')]
        assert len(want) == 1
    else:
        want = [f'{prefix}:{g}' for g in (got if isinstance(got, list) else [got])]
    res = _run(path)
    assert res[inv]['violators'] == want, res[inv]
    assert res[inv]['ok'] is False
    others = {k for k, r in res.items() if r['violators'] and k != inv}
    assert others == also, {k: res[k]['violators'] for k in others}


def test_i14_pinned_ids_are_reported_but_not_a_failure(db):
    path, ids = db
    pinned = min(oracle.EXPECTED['I14'])
    _exec(path, "INSERT INTO cashbook_transactions (id, account_id, txn_date, direction, category, "
                "amount) VALUES (?, ?, '2026-03-01', 'expense', ?, 100.0)",
          (pinned, ids['acct_392'], scn.SALARY_CATEGORY))
    res = _run(path)['I14']
    assert res['violators'] == [f'txn:{pinned}'] and res['unexpected'] == [] and res['ok']
    assert '1 of 29 pinned + 0 unpinned' in res['value']


def test_mirror_conflict_key_is_not_drift(db):
    """A payout the mirror would skip (a matching manual row within 2 days)
    is expected to be unmirrored."""
    path, ids = db
    _exec(path, "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders) "
                "VALUES ('shopee', '2026-08-25', 800.0, 2)")
    assert _run(path)['I6']['violators'] == ['shopee:missing:2026-08-25/800.0/1']
    _txn(path, ids, account_id=ids['acct_SPX'], direction='income', category=scn.PAYOUT_CATEGORY,
         amount=800.0, txn_date='2026-08-26')
    res = _run(path)['I6']
    assert res['violators'] == [] and res['value'] == 'lazada 0 = 0, shopee 2 = 2'


# ── the copies the stdlib-only oracle keeps must equal the app's ───────────

def test_copies_equal_the_app():
    import cashbook_ledger as ledger
    import cashbook_payout_mirror as mirror
    assert oracle.ISO_DATE_PATTERN == ledger.ISO_DATE_PATTERN
    for name in ('ADVANCE_CATEGORY', 'SALARY_CATEGORY', 'COMMISSION_CATEGORY', 'PAYOUT_CATEGORY',
                 'PAYOUT_CREATED_BY', 'MIN_DEPOSIT_DATE', 'PLATFORM_ACCOUNT_CODE'):
        assert getattr(oracle, name) == getattr(ledger, name), name
    assert oracle.CONFLICT_WINDOW_DAYS == mirror.CONFLICT_WINDOW_DAYS


def test_copied_algorithms_agree_with_the_app(db):
    import cashbook_ledger as ledger
    import cashbook_payout_mirror as mirror
    path, ids = db
    _exec(path, "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders) "
                "VALUES ('shopee', '2026-08-15', 1500.004, 1)")
    c = database._connect(path)
    try:
        assert oracle.payout_target(c, 'shopee') == ledger.payout_target(c, 'shopee')
        assert len(oracle.payout_target(c, 'shopee')) == 3
        for args in ((ids['acct_SPX'], '2026-08-16', 1500.0), (ids['acct_SPX'], '2026-08-30', 1.0)):
            assert (oracle.conflicting_manual_row_exists(c, *args)
                    == mirror._conflicting_manual_row_exists(c, *args))
    finally:
        c.close()


def test_oracle_is_stdlib_only():
    tree = ast.parse(open(SCRIPT, encoding='utf-8').read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or '').split('.')[0])
    assert mods and mods <= {'argparse', 'hashlib', 'json', 'math', 'os', 're', 'sqlite3', 'sys',
                             'datetime'}, mods


def test_cli_json_and_exit_code(db):
    """Run it the way prod will: a separate process, stdlib python, read-only."""
    path, ids = db
    ok = subprocess.run([sys.executable, SCRIPT, '--db', path, '--json'],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    report = json.loads(ok.stdout)
    assert len(report['invariants']) == len(oracle.INVARIANTS)
    # the I14 pin carries where and when it was read, and its count is the set's
    assert report['pins']['I14'] == {'env': 'PROD', 'read': '2026-09-30',
                                     'count': len(oracle.EXPECTED['I14'])}
    _txn(path, ids, category='หมวดที่ไม่มี')
    bad = subprocess.run([sys.executable, SCRIPT, '--db', path], capture_output=True, text=True)
    assert bad.returncode == 1 and 'BAD I9' in bad.stdout, bad.stdout
    assert 'pin I14: 29 ids read on PROD 2026-09-30' in bad.stdout, bad.stdout


def test_oracle_never_writes(db):
    path, _ = db
    c = oracle.connect_ro(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            c.execute("DELETE FROM cashbook_transactions")
    finally:
        c.close()
