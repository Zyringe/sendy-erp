"""Card F census: every place that writes `cashbook_transactions`, by file and
exact hit count (plan §5 PR-1).

Scans the raw source of every `inventory_app/**/*.py` (minus tests, __pycache__,
instance, static) and `scripts/**/*.py`, whitespace runs collapsed, against one
case-insensitive pattern. An allowed file with one hit more than its count goes
red, as does any file not in the map. PR-2 removes `blueprints/cashbook.py`,
PR-3 leaves only the ledger and the one-off scripts.

What this cannot see (stated, not caught): a table name built at runtime
(`f"DELETE FROM {tbl}"`, concatenation, `.format()`). The reviewer's
`git grep` and the invariant oracle (`scripts/audit_cashbook_invariants.py`)
are the nets for that.
"""
import os
import re

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

WRITE_RE = re.compile(
    r"""(insert(\s+or\s+\w+)?\s+into|replace\s+into|update(\s+or\s+\w+)?|delete\s+from)"""
    r"""\s+["'`\w.]*cashbook_transactions\b""",
    re.IGNORECASE,
)

_SKIP_DIRS = {'tests', '__pycache__', 'instance', 'static'}

# {repo-relative path: (exact hit count, reason)}
ALLOWED = {
    'inventory_app/blueprints/cashbook.py': (
        4, "manual/advance INSERT x2, edit UPDATE, delete DELETE; moves to the ledger in PR-2"),
    'inventory_app/hr.py': (
        2, "salary pay-event INSERT + void DELETE; moves to the ledger in PR-3"),
    'inventory_app/commission.py': (
        2, "commission payout INSERT + cancel DELETE; moves to the ledger in PR-3"),
    'inventory_app/cashbook_payout_mirror.py': (
        4, "mirror INSERT, DELETE, UPDATE description + the module docstring's "
           "'Insert/delete/update cashbook_transactions' prose; PR-3 rewords it"),
    'inventory_app/cashbook_ledger.py': (
        11, "the seam: post_manual, post_advance, post_salary, post_commission, post_payout "
            "INSERT; amend_manual, set_payout_description UPDATE; cancel_manual, "
            "cancel_salary, cancel_commission, cancel_payout DELETE"),
    'scripts/convert_legacy_cashbook_payout_rows.py': (
        1, "one-time ADR 0013 conversion (UPDATE of the payout_* columns)"),
    'scripts/2026_09_19_589_commission_03_backrecord.py': (
        1, "one-time #589 back-record (DELETE), rehearse/live flag"),
}


def _files(root):
    app = os.path.join(root, 'inventory_app')
    for base in (app, os.path.join(root, 'scripts')):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames
                                 if not (base == app and d in _SKIP_DIRS) and d != '__pycache__')
            for f in sorted(filenames):
                if f.endswith('.py'):
                    yield os.path.join(dirpath, f)


def census(root):
    """{repo-relative path: hit count} for every file with at least one hit."""
    out = {}
    for path in _files(root):
        with open(path, encoding='utf-8') as fh:
            src = re.sub(r'\s+', ' ', fh.read())
        n = len(WRITE_RE.findall(src))
        if n:
            out[os.path.relpath(path, root)] = n
    return out


def test_every_cashbook_writer_is_in_the_count_map():
    found = census(REPO)
    assert 'inventory_app/blueprints/cashbook.py' in found, "control: the census saw nothing"
    assert found == {path: n for path, (n, _) in ALLOWED.items()}


def test_the_census_scans_the_places_it_claims():
    """Control for the walk: each scanned root yields files, the skipped
    directories yield none."""
    files = [os.path.relpath(p, REPO) for p in _files(REPO)]
    assert any(f.startswith('inventory_app/blueprints/') for f in files)
    assert any(f.startswith('scripts/') for f in files)
    assert not [f for f in files if f.startswith(('inventory_app/tests/', 'inventory_app/static/',
                                                  'inventory_app/instance/'))]


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


def _mini_tree(tmp_path, rel, body):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return str(tmp_path)


@pytest.mark.parametrize('shape', sorted(_SHAPES))
@pytest.mark.parametrize('rel', ['inventory_app/models/new_writer.py', 'scripts/oneoff_fix.py'])
def test_each_shape_is_caught_in_a_new_file(tmp_path, shape, rel):
    root = _mini_tree(tmp_path, rel, _SHAPES[shape])
    assert census(root) == {rel: 1}


def test_an_extra_write_in_an_allowed_file_is_caught(tmp_path):
    rel = 'inventory_app/hr.py'
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        src = fh.read()
    root = _mini_tree(tmp_path, rel, src + '\n' + _SHAPES['lowercase'])
    assert census(root)[rel] == ALLOWED[rel][0] + 1


def test_skipped_directories_really_are_skipped(tmp_path):
    root = _mini_tree(tmp_path, 'inventory_app/static/x.py', _SHAPES['lowercase'])
    assert census(root) == {}


def test_a_runtime_built_table_name_is_not_caught_as_documented(tmp_path):
    """Pins the stated blind spot, so nobody reads a green census as covering it."""
    root = _mini_tree(tmp_path, 'inventory_app/models/dyn.py',
                      'tbl = "cashbook_transactions"\nsql = f"DELETE FROM {tbl} WHERE id = 1"\n')
    assert census(root) == {}
