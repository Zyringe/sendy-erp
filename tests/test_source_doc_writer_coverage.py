"""Every writer of the two source-document tables must be a decision on the record.

WHY THIS FILE EXISTS
    mig 173 makes an undeclared UPDATE of a meaningful column abort, which turns
    "did I find every writer?" from tidiness into a runtime failure. I got it
    wrong three times: a hand grep missed `repoint_bsn_code` (/mapping/split-save
    would abort), then `learn_acronyms_normalize` (/unit-conversions, same), then
    `dismiss_pending_unit_conversion` (a human delete recorded as the importer).
    Review caught all three; re-reading my own list never did.

    ⚠ AND THE FIRST VERSION OF THIS SWEEP WAS ITSELF TOO NARROW. It matched only
    `UPDATE <literal table> SET`, so a table alias, a concatenated string, a
    `.format()`, or anything under scripts/ walked straight past it. A sweep that
    cannot see a shape is worse than no sweep, because it reads as coverage. The
    normalisation below exists to flatten those shapes before matching, and
    `test_the_sweep_sees_every_shape` feeds it each one.

⛔ WHAT THIS SWEEP STILL CANNOT SEE — stated so nobody reads it as proof:
    · SQL assembled at runtime from pieces that are never adjacent literals
      (`" ".join((...))`, a constant imported from another module, a table name
      that arrives as a function argument);
    · SQL living in a file that is not `.py`;
    · anything reached through an ORM or a query builder.
    It is a net for the shapes this codebase actually uses, not a proof of
    completeness. The DB guard is what makes an undeclared UPDATE fail; this only
    makes it fail in CI instead of in front of an operator.
"""
import ast
import os
import re

import pytest

HERE = os.path.dirname(__file__)
APP = os.path.join(HERE, '..', 'inventory_app')
SCRIPTS = os.path.join(HERE, '..', 'scripts')
TABLE_RE = r'(?:sales_transactions|purchase_transactions|\{table\}|\{t\}|\{tbl\}|\{\})'

# Adjacent Python string literals, `+` concatenation and `.format()` all break a
# naive SQL regex. Flatten them first so the matcher sees one statement.
_JOIN = re.compile(r'''["']\s*(?:\+|\\)?\s*(?:f|r|b)?["']''')
_FORMAT = re.compile(r'''["']\s*\.\s*format\s*\(\s*''')


def strip_prose(src):
    """Blank out docstrings and `#` comments before matching.

    Not cosmetic: import_credit_notes.py's module docstring says "Do NOT insert
    into sales_transactions" while explaining why a new SR goes elsewhere, and a
    case-insensitive whitespace-collapsed scan reads that sentence as an INSERT.
    The repo's older sweep allowlisted that file as "a false positive from its
    own docstring" — an exemption that would also hide a REAL writer added to it
    later. Removing the prose is the fix; allowlisting the lie is not.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, 'body', None)
        if not body or not isinstance(body[0], ast.Expr):
            continue
        val = body[0].value
        if not (isinstance(val, ast.Constant) and isinstance(val.value, str)):
            continue
        for i in range(val.lineno - 1, min(val.end_lineno, len(lines))):
            lines[i] = '\n'
    out = ''.join(lines)
    return re.sub(r'(?m)#.*$', '', out)


def normalise(src):
    src = strip_prose(src)
    src = _FORMAT.sub('"', src)
    src = _JOIN.sub('', src)
    return re.sub(r'\s+', ' ', src)


# `UPDATE OR REPLACE`, `main.sales_transactions`, and an alias all appear in real
# SQL. The SET clause is captured WHOLE and every column pulled out of it: the
# first version took only the first column, so
# `SET synced_to_stock = 0, customer_code = ?` read as exempt and vanished —
# which is the exact shape reconcile.py's own stamp uses (Codex round 4).
_QUAL = r'(?:[a-z_]+\.)?'
UPDATE_RE = re.compile(
    rf'\bUPDATE\s+(?:OR\s+\w+\s+)?{_QUAL}{TABLE_RE}(?:\s+(?:AS\s+)?[a-z]\w*)?'
    rf'\s+SET\s+(.*?)(?:\s+WHERE\b|\s+RETURNING\b|["\';]|$)', re.I)
DELETE_RE = re.compile(rf'\bDELETE\s+FROM\s+(?:OR\s+\w+\s+)?{_QUAL}{TABLE_RE}', re.I)
INSERT_RE = re.compile(rf'\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+{_QUAL}{TABLE_RE}', re.I)
SET_COL_RE = re.compile(r'([a-z_]+)\s*=', re.I)

EXEMPT_COLS = {'synced_to_stock', 'batch_id', 'created_at',
               'change_source', 'change_actor', 'change_reason', 'change_token'}

# Files that route their guarded writes through _shared.declared_update /
# declared_delete. They must NOT appear in the sweep — if one does, a raw SQL
# writer has been added beside the helper call.
VIA_HELPER = {
    'models/mapping.py':
        'resolve_pending_mappings and repoint_bsn_code both call declared_update; '
        'the second is reachable from /mapping/split-save',
    'import_credit_notes.py':
        'the ref_invoice backfill during a credit-note import calls declared_update '
        'with actor=credit-notes-backfill',
}

# ⛔ A bare string is not a reason. Name the route or the caller, and what it sets.
DECLARED = {
    'models/imports.py':
        'the importer. INSERTs stamp change_source=import plus the batch token; a '
        'changed line is replaced by DELETE+INSERT, which the UPDATE guard never sees',
    'models/bsn_sync.py':
        'learn_acronyms_normalize bulk-rewrites unit from /unit-conversions and '
        'declares inline; dismiss_pending_unit_conversion stamps the operator '
        'before deleting; every other write here touches synced_to_stock only',
    'models/reconcile.py':
        'apply_reconcile_flag stamps the human resolver onto the rows before it '
        'deletes them, so the DELETE audit row is not attributed to the importer',
    'database.py':
        'the one-shot doc_base backfill inside init_db for a DB old enough to lack '
        'the column; it declares change_source=import inline',
    'blueprints/admin.py':
        'the /admin/upload-db restore path, which wipes and repopulates EVERY '
        'table from an uploaded DB file (`DELETE FROM main.{table}` then '
        '`INSERT INTO main.{table}`). It is a whole-database replacement, not a '
        'row edit: the change_source each row carries is whatever the uploaded '
        'file held, and the guard cannot meaningfully apply because there is no '
        'per-row human decision to record.',
    'models/_shared.py':
        'declared_update and declared_delete themselves: this file IS the mechanism '
        'every other declared site calls, so it writes by definition',
}

# Historical one-off scripts. They pre-date the guard and would abort if re-run;
# that is ACCEPTED, not overlooked — they are archived evidence of past data ops,
# not runtime paths, and rewriting them would falsify what was actually run.
# ⚠ Anything that is a real TOOL rather than a dated one-off does not belong here.
SCRIPT_EXEMPTIONS = {
    # Dated one-off data ops that write a GUARDED column. They pre-date the guard
    # and would abort if re-run;
    # that is ACCEPTED, not overlooked — rewriting them would falsify the record
    # of what was actually executed. ⚠ A real TOOL does not belong here, and
    # neither does a script that only ever touches synced_to_stock: five such
    # entries were removed on the staleness check's first run, because a list
    # nobody prunes stops being an inventory.
    'apply_decision_remaps.py': 'one-off remap of decided product mappings',
    'apply_worksheets_20260530.py': 'one-off apply of the 2026-05-30 worksheets',
    'apply_stock_and_mapping_csv.py': 'one-off apply of a stock and mapping CSV worksheet',
    'cleanup_split_mapping_stubs.py': 'one-off cleanup of stub rows left by a mapping split',
    'normalize_bsn_units.py': 'one-off unit normalisation; now learn_acronyms_normalize',
    'p0_split_3p5in.py': 'one-off split of the 3.5in pack and piece SKUs',
    'p2p3_split_hinges.py': 'one-off split of the hinge pack and piece SKUs',
    'phase_c_replay_apply_20260530.py': 'one-off apply of the phase-C ledger replay',
    'phase_c_dedup_replay_20260530.py': 'one-off dedup pass of the phase-C replay',
    'load_purchase_history_20250529.py': 'one-off initial load of the purchase history',
    'backfill_nonstock_2026_08.py': 'one-off non-stock line backfill (mig 155 era)',
    'reimport_2026_04_28/import_credit_notes.py': 'archived copy of the 04-28 reimport',
    'reimport_2026_04_28/run.py': 'archived driver for the 04-28 reimport',
}


# A `{table}` / `{t}` placeholder is only OURS if the file binds it to a source
# table. import_router.py loops `for t in ("express_gl_lines", ...)` and deletes
# from `{t}` — a real DELETE, on tables that are none of our business. Without
# this the sweep reports it forever and everyone learns to add exemptions.
BINDS_SOURCE_TABLE = re.compile(
    r"""["'](?:sales|purchase)_transactions["']""")
DYNAMIC = re.compile(r'\{table\}|\{t\}|\{tbl\}|\{\}')
LITERAL = re.compile(r'\b(?:sales|purchase)_transactions\b')


def _relevant(flat, match_text):
    """Does this hit actually concern a source-document table?"""
    if LITERAL.search(match_text):
        return True
    return bool(DYNAMIC.search(match_text) and BINDS_SOURCE_TABLE.search(flat))


def _scan(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'templates')]
        for f in files:
            if not f.endswith('.py'):
                continue
            path = os.path.join(dirpath, f)
            with open(path, encoding='utf-8') as fh:
                flat = normalise(fh.read())
            cols = set()
            for m in UPDATE_RE.finditer(flat):
                if _relevant(flat, m.group(0)):
                    cols |= {c.lower() for c in SET_COL_RE.findall(m.group(1))}
            cols -= EXEMPT_COLS
            dels = any(_relevant(flat, m.group(0)) for m in DELETE_RE.finditer(flat))
            ins = any(_relevant(flat, m.group(0)) for m in INSERT_RE.finditer(flat))
            if cols or dels or ins:
                rel = os.path.relpath(path, root).replace(os.sep, '/')
                out[rel] = {'update_cols': sorted(cols), 'deletes': dels, 'inserts': ins}
    return out


def test_every_app_writer_is_accounted_for():
    found = _scan(APP)
    # CONTROL: a broken regex or walk empties `found` and every assertion below
    # passes vacuously — the shape this repo keeps re-learning.
    assert found, 'the sweep found no writers at all — the sweep is broken'
    assert 'models/imports.py' in found, found      # the importer must always appear
    undeclared = {f: v for f, v in found.items()
                  if f not in DECLARED and f not in VIA_HELPER}
    assert not undeclared, (
        'these write or delete source-document rows and are not in DECLARED. '
        'Under mig 173 an undeclared UPDATE aborts, and an unstamped DELETE is '
        f'attributed to whoever last wrote the row: {undeclared}')


# What each declared file is KNOWN to write. Pinned, because file-level
# allowlisting alone lets a new rogue writer hide inside a file that is already
# declared (Codex round 4) — the file passes, the new column never gets a look.
DECLARED_COLUMNS = {
    'models/imports.py': [],
    'models/bsn_sync.py': ['unit'],
    'models/reconcile.py': [],
    'database.py': ['doc_base'],
    'blueprints/admin.py':
        'the /admin/upload-db restore path, which wipes and repopulates EVERY '
        'table from an uploaded DB file (`DELETE FROM main.{table}` then '
        '`INSERT INTO main.{table}`). It is a whole-database replacement, not a '
        'row edit: the change_source each row carries is whatever the uploaded '
        'file held, and the guard cannot meaningfully apply because there is no '
        'per-row human decision to record.',
    'models/_shared.py': [],
}


def test_helper_routed_files_contain_no_raw_writer():
    """The other half of the allowlist. A DECLARED entry the sweep never detects
    passes for the wrong reason — 'this file is known' instead of 'this writer is
    declared' (Codex round 4). These files must come back CLEAN; if one appears,
    someone added raw SQL next to the helper call."""
    found = _scan(APP)
    assert found, 'the sweep found nothing — it is broken'
    leaked = {f: found[f] for f in VIA_HELPER if f in found}
    assert not leaked, (
        'these route through declared_update and must contain no raw guarded '
        f'write, but the sweep found one: {leaked}')


def test_every_declared_file_is_actually_detected():
    """And the first half: a file listed in DECLARED that the sweep cannot see is
    an allowlist entry protecting nothing."""
    found = _scan(APP)
    invisible = sorted(set(DECLARED) - set(found) - set(VIA_HELPER))
    assert not invisible, (
        'listed in DECLARED but the sweep does not detect any write there — the '
        f'entry is either stale or the sweep is blind to its shape: {invisible}')


def test_no_new_guarded_column_appears_inside_a_declared_file():
    found = _scan(APP)
    assert found, 'the sweep found nothing — it is broken'
    drift = {f: sorted(set(v['update_cols']) - set(DECLARED_COLUMNS.get(f, [])))
             for f, v in found.items()}
    drift = {f: c for f, c in drift.items() if c}
    assert not drift, (
        'a guarded column is written in a file that was declared for different '
        f'columns — each one needs its own decision: {drift}')


def test_every_script_writer_is_either_converted_or_knowingly_exempt():
    found = _scan(SCRIPTS)
    assert found, 'the script sweep found nothing — it is broken'
    unknown = sorted(set(found) - set(SCRIPT_EXEMPTIONS))
    assert not unknown, (
        'new or unlisted scripts write source-document rows. A dated one-off can '
        f'join SCRIPT_EXEMPTIONS; a real tool must declare instead: {unknown}')


def test_script_exemptions_are_not_stale_and_carry_reasons():
    """An exemption list nobody prunes stops being an inventory and becomes
    ceremony. Every entry must still be a file that actually writes."""
    found = _scan(SCRIPTS)
    for name, reason in SCRIPT_EXEMPTIONS.items():
        assert len(reason.split()) >= 4, f'{name}: not a reason: {reason!r}'
    stale = sorted(n for n in SCRIPT_EXEMPTIONS
                   if n not in found and os.path.exists(os.path.join(SCRIPTS, n)))
    assert not stale, (
        'listed as exempt but the sweep no longer sees them writing — either the '
        f'script changed or the sweep went blind: {stale}')
    gone = sorted(n for n in SCRIPT_EXEMPTIONS
                  if not os.path.exists(os.path.join(SCRIPTS, n)))
    assert not gone, f'exempted scripts that no longer exist: {gone}'


def test_every_exemption_carries_a_real_reason():
    for site, reason in DECLARED.items():
        assert len(reason.split()) >= 10, f'{site}: not a reason: {reason!r}'
        assert not reason.strip().lower().startswith(('todo', 'fix', 'legacy')), site


@pytest.mark.parametrize('shape', [
    'conn.execute("UPDATE sales_transactions SET customer_code=? WHERE id=?")',
    'conn.execute("UPDATE sales_transactions AS t SET t.customer_code=? WHERE id=?")',
    'table = "sales_transactions"\nconn.execute(f"UPDATE {table} SET customer_code=? WHERE id=?")',
    'conn.execute("UPDATE " "sales_transactions" " SET customer_code=? WHERE id=?")',
    'table = "purchase_transactions"\nconn.execute("UPDATE {} SET customer_code=? WHERE id=?".format(table))',
    'conn.execute("DELETE FROM purchase_transactions WHERE id=?")',
    'conn.executemany("UPDATE purchase_transactions SET net=? WHERE id=?", rows)',
])
def test_the_sweep_sees_every_shape(shape, tmp_path):
    """break-it-once, one shape per case. The first version of this sweep passed
    on five of these seven while claiming to cover the codebase."""
    (tmp_path / 'rogue.py').write_text(shape + '\n', encoding='utf-8')
    found = _scan(str(tmp_path))
    assert found, f'the sweep does not see this shape: {shape}'


def test_the_sweep_ignores_a_dynamic_table_that_is_not_ours(tmp_path):
    """import_router.py deletes from `{t}` where t iterates express_gl_* tables.
    A sweep that flags it teaches everyone to add exemptions until the list means
    nothing."""
    (tmp_path / 'other.py').write_text(
        'for t in ("express_gl_lines", "express_gl_vouchers"):\n'
        '    conn.execute(f"DELETE FROM {t} WHERE entity = ?", (entity,))\n',
        encoding='utf-8')
    assert _scan(str(tmp_path)) == {}


def test_the_sweep_ignores_a_read(tmp_path):
    """CONTROL for the above: if it flagged everything, the shape cases would
    pass for free."""
    (tmp_path / 'reader.py').write_text(
        'conn.execute("SELECT net FROM sales_transactions WHERE id=?")\n', encoding='utf-8')
    assert _scan(str(tmp_path)) == {}
