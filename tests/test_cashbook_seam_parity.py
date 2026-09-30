"""Card F PR-2: the parity scenario is not vacuous (plan §3f).

The gate itself is the two-worktree differential,
`scripts/dev/cashbook_parity_diff.py <base-sha>`: both trees build the same DB
from their own `schema.sql`, run `cashbook_seam_scenario.run`, and the dumps
must be identical. This file pins what makes that comparison worth anything:
  - every module snapshot of DATABASE_PATH is redirected (W1): the round-2
    blindness was `/hr/.../pay` and `/commission/payout` writing elsewhere;
  - the run reaches every kind, with the counts the plan's arithmetic gives;
  - the route validators still report EVERY error (N2): the ledger raises only
    the first refusal, so a validator deleted in favour of the ledger would
    shrink these lists.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import ast

import pytest

# Imported at the top on purpose (W1): a module first imported AFTER redirect()
# would take its snapshot from config at that moment and hide a missing patch.
import cashbook_payout_mirror  # noqa: F401
import commission  # noqa: F401
import hr  # noqa: F401

import database
from tests import cashbook_seam_scenario as scn

CONFTEST = os.path.join(os.path.dirname(__file__), 'conftest.py')


def _tmp_db_patched_modules():
    """The modules whose DATABASE_PATH `tmp_db` monkeypatches, read from
    conftest.py's AST (the set this scenario must mirror)."""
    tree = ast.parse(open(CONFTEST, encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'tmp_db')
    out = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'setattr' and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == 'DATABASE_PATH'):
            out.add(node.args[0].id)
    return out


def test_redirect_covers_exactly_what_tmp_db_patches():
    patched = _tmp_db_patched_modules()
    assert len(patched) == 7, f"control: the AST read found {sorted(patched)}"
    assert set(scn.REDIRECTED_MODULES) == patched


def test_redirect_points_all_seven_snapshots(tmp_path, monkeypatch):
    import importlib
    path = str(tmp_path / 'x.db')
    for name in scn.REDIRECTED_MODULES:
        assert getattr(importlib.import_module(name), 'DATABASE_PATH') != path
    scn.redirect(monkeypatch.setattr, path)
    got = {name: importlib.import_module(name).DATABASE_PATH for name in scn.REDIRECTED_MODULES}
    assert len(got) == 7 and set(got.values()) == {path}, got


@pytest.fixture(scope='module')
def result(tmp_path_factory):
    """One full scenario run on a fresh DB (module-scoped: it is ~40 requests)."""
    mp = pytest.MonkeyPatch()
    try:
        path = str(tmp_path_factory.mktemp('parity') / 'scenario.db')
        scn.build_db(path)
        scn.redirect(mp.setattr, path)
        conn = database._connect(path)
        try:
            ids = scn.seed(conn)
            from app import app as flask_app
            flask_app.config['TESTING'] = True
            client = flask_app.test_client()
            with client.session_transaction() as s:
                s.update(scn.ADMIN_SESSION)
            steps = scn.run(client, conn, ids)
            conn.commit()
            yield scn.dump(conn, steps)
        finally:
            conn.close()
    finally:
        mp.undo()


def test_every_kind_is_reached(result):
    assert result['kind_counts'] == scn.EXPECTED_COUNTS
    for table, n in scn.EXPECTED_TABLE_COUNTS.items():
        assert len(result[table]) == n, table


def test_attributed_audit_rows(result):
    got = {}
    for a in result['audit_log']:
        if a['user'] is not None:
            got[a['table_name']] = got.get(a['table_name'], 0) + 1
    assert got == scn.EXPECTED_ATTRIBUTED_AUDIT


def _step(result, name):
    hits = [s for s in result['steps'] if s['step'] == name]
    assert len(hits) == 1, name
    return hits[0]


def test_n2_new_reports_every_error_and_saves_nothing(result):
    s = _step(result, 'new multi-error')
    assert s['status'] == 200
    assert s['flashes'] == [['กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่', 'danger']]
    assert s['row_errors'] == [
        'จำนวนเงินต้องมากกว่า 0, ประเภทไม่ถูกต้อง, กรุณาระบุหมวดหมู่',
        'รูปแบบวันที่ไม่ถูกต้อง',
        'จำนวนเงินต้องมากกว่า 0',
    ]
    assert not [r for r in result['cashbook_transactions'] if r['amount'] in (5.0,)]


def test_n2_edit_reports_every_error_and_changes_nothing(result):
    hits = [s for s in result['steps'] if s['step'].startswith('edit ')
            and s.get('flashes') and all(c == 'danger' for _, c in s['flashes'])
            and len(s['flashes']) > 1]
    assert len(hits) == 1, "control: exactly one multi-error edit step"
    assert hits[0]['status'] == 302
    assert hits[0]['flashes'] == [['รูปแบบวันที่ไม่ถูกต้อง', 'danger'],
                                  ['จำนวนเงินต้องมากกว่า 0', 'danger']]


def test_locked_kinds_are_refused_on_edit_and_delete(result):
    refused = [s for s in result['steps'] if s.get('status') == 403]
    assert len(refused) == 8, [s['step'] for s in refused]
