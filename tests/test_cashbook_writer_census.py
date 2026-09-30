"""Card F census: every place that writes `cashbook_transactions`, by file and
exact hit count (plan §5 PR-1).

Scans the raw source of every `inventory_app/**/*.py` (minus __pycache__,
instance, static) and `scripts/**/*.py` through the shared `tests/_census.py`
walker, whitespace runs collapsed, against one case-insensitive pattern. An allowed file with one hit more than its count goes
red, as does any file not in the map. PR-2 removed `blueprints/cashbook.py`
(its writes go through the ledger); PR-3 leaves only the ledger and the
one-off scripts.

What this cannot see (stated, not caught): a table name built at runtime
(`f"DELETE FROM {tbl}"`, concatenation, `.format()`). The reviewer's
`git grep` and the invariant oracle (`scripts/audit_cashbook_invariants.py`)
are the nets for that.
"""
import os
import re

import pytest

from tests import _census

REPO = _census.ROOT

WRITE_RE = re.compile(
    r"""(insert(\s+or\s+\w+)?\s+into|replace\s+into|update(\s+or\s+\w+)?|delete\s+from)"""
    r"""\s+["'`\w.]*cashbook_transactions\b""",
    re.IGNORECASE,
)

# {repo-relative path: (exact hit count, reason)}
ALLOWED = {
    'cashbook_ledger.py': (
        11, "the seam: post_manual, post_advance, post_salary, post_commission, post_payout "
            "INSERT; amend_manual, set_payout_description UPDATE; cancel_manual, "
            "cancel_salary, cancel_commission, cancel_payout DELETE"),
    'scripts/convert_legacy_cashbook_payout_rows.py': (
        1, "one-time ADR 0013 conversion (UPDATE of the payout_* columns)"),
    'scripts/2026_09_19_589_commission_03_backrecord.py': (
        1, "one-time #589 back-record (DELETE), rehearse/live flag"),
    'scripts/2026-09-30_redate_cashbook_642.py': (
        1, "one-time card F PR-0 (Q7 B) re-date of row 642 (UPDATE), --apply gate"),
}


def census():
    """{path: hit count} for every file with at least one hit. Paths are
    relative to inventory_app/, scripts prefixed 'scripts/' (the shared
    `tests/_census.py` walker). Raw source on purpose: a docstring that names a
    write counts, so rewording it is a visible change."""
    out = {}
    for rel, path in _census.py_files(include_scripts=True):
        n = len(WRITE_RE.findall(re.sub(r'\s+', ' ', _census.read(path))))
        if n:
            out[rel] = n
    return out


def test_every_cashbook_writer_is_in_the_count_map():
    found = census()
    assert 'cashbook_ledger.py' in found, "control: the census saw nothing"
    assert found == {path: n for path, (n, _) in ALLOWED.items()}


def test_the_cashbook_blueprint_writes_nothing_itself():
    """Card F PR-2 done-condition: no write SQL left in blueprints/cashbook.py.
    A CLEAN site, pinned by name (a file-level allowlist cannot say this)."""
    assert 'blueprints/cashbook.py' not in census()
    src = _census.read(os.path.join(REPO, 'inventory_app', 'blueprints', 'cashbook.py'))
    assert 'cashbook_ledger.post_manual' in src, "control: the blueprint was read"


def test_the_census_scans_the_places_it_claims():
    """Control for the walk: each scanned root yields files, the skipped
    directories yield none."""
    files = [rel for rel, _ in _census.py_files(include_scripts=True)]
    assert any(f.startswith('blueprints/') for f in files)
    assert any(f.startswith('scripts/') for f in files)
    assert not [f for f in files if f.startswith(('static/', 'instance/'))]


# ── break once, per shape ────────────────────────────────────────────────────

_SHAPES = {
    'triple-quoted multi-line': 'x = """\n    INSERT INTO\n        cashbook_transactions (a) VALUES (1)\n"""\n',
    'lowercase': 'x = "insert into cashbook_transactions (a) values (1)"\n',
    'INSERT OR IGNORE': 'x = "INSERT OR IGNORE INTO cashbook_transactions (a) VALUES (1)"\n',
    'UPDATE OR REPLACE': 'x = "UPDATE OR REPLACE cashbook_transactions SET a = 1"\n',
    'double-quoted table': "x = 'UPDATE \"cashbook_transactions\" SET a = 1'\n",
    'schema-qualified': 'x = "DELETE FROM main.cashbook_transactions WHERE id = 1"\n',
    'REPLACE INTO': 'x = "REPLACE INTO cashbook_transactions (a) VALUES (1)"\n',
}


@pytest.fixture
def mini(tmp_path, monkeypatch):
    """Point the shared walker at an empty tree; returns a writer for it."""
    monkeypatch.setattr(_census, 'APP', str(tmp_path / 'inventory_app'))
    monkeypatch.setattr(_census, 'SCRIPTS', str(tmp_path / 'scripts'))

    def put(rel, body):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
    return put


@pytest.mark.parametrize('shape', sorted(_SHAPES))
@pytest.mark.parametrize('rel', ['inventory_app/models/new_writer.py', 'scripts/oneoff_fix.py'])
def test_each_shape_is_caught_in_a_new_file(mini, shape, rel):
    mini(rel, _SHAPES[shape])
    assert census() == {rel.replace('inventory_app/', ''): 1}


def test_an_extra_write_in_an_allowed_file_is_caught(mini):
    src = _census.read(os.path.join(REPO, 'inventory_app', 'cashbook_ledger.py'))
    mini('inventory_app/cashbook_ledger.py', src + '\n' + _SHAPES['lowercase'])
    assert census()['cashbook_ledger.py'] == ALLOWED['cashbook_ledger.py'][0] + 1


def test_skipped_directories_really_are_skipped(mini):
    mini('inventory_app/static/x.py', _SHAPES['lowercase'])
    mini('inventory_app/models/control.py', _SHAPES['lowercase'])
    assert census() == {'models/control.py': 1}


def test_a_runtime_built_table_name_is_not_caught_as_documented(mini):
    """Pins the stated blind spot, so nobody reads a green census as covering it."""
    mini('inventory_app/models/dyn.py',
         'tbl = "cashbook_transactions"\nsql = f"DELETE FROM {tbl} WHERE id = 1"\n')
    assert census() == {}
