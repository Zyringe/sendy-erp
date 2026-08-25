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
"""
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


def normalise(src):
    src = _FORMAT.sub('"', src)
    src = _JOIN.sub('', src)
    return re.sub(r'\s+', ' ', src)


UPDATE_RE = re.compile(
    rf'\bUPDATE\s+{TABLE_RE}(?:\s+(?:AS\s+)?[a-z]\w*)?\s+SET\s+([a-z_]+)', re.I)
DELETE_RE = re.compile(rf'\bDELETE\s+FROM\s+{TABLE_RE}', re.I)

EXEMPT_COLS = {'synced_to_stock', 'batch_id', 'created_at',
               'change_source', 'change_actor', 'change_reason', 'change_token'}

# ⛔ A bare string is not a reason. Name the route or the caller, and what it sets.
DECLARED = {
    'models/imports.py':
        'the importer. INSERTs stamp change_source=import plus the batch token; a '
        'changed line is replaced by DELETE+INSERT, which the UPDATE guard never sees',
    'models/mapping.py':
        'resolve_pending_mappings and repoint_bsn_code both route through '
        '_shared.declared_update (actor mapping-resolve / repoint-bsn-code); the '
        'second is reachable from /mapping/split-save',
    'models/bsn_sync.py':
        'learn_acronyms_normalize bulk-rewrites unit from /unit-conversions and '
        'declares inline; dismiss_pending_unit_conversion stamps the operator '
        'before deleting; every other write here touches synced_to_stock only',
    'models/reconcile.py':
        'apply_reconcile_flag stamps the human resolver onto the rows before it '
        'deletes them, so the DELETE audit row is not attributed to the importer',
    'import_credit_notes.py':
        'the ref_invoice backfill run while importing a credit-note file, which '
        'declares actor=credit-notes-backfill on the row it touches',
    'database.py':
        'the one-shot doc_base backfill inside init_db for a DB old enough to lack '
        'the column; it declares change_source=import inline',
    'models/_shared.py':
        'declared_update and declared_delete themselves: this file IS the mechanism '
        'every other declared site calls, so it writes by definition',
}

# Historical one-off scripts. They pre-date the guard and would abort if re-run;
# that is ACCEPTED, not overlooked — they are archived evidence of past data ops,
# not runtime paths, and rewriting them would falsify what was actually run.
# ⚠ Anything that is a real TOOL rather than a dated one-off does not belong here.
SCRIPT_EXEMPTIONS = {
    'apply_decision_remaps.py', 'apply_worksheets_20260530.py',
    'cleanup_split_mapping_stubs.py', 'merge_product.py', 'normalize_bsn_units.py',
    'p0_split_3p5in.py', 'p2p3_split_hinges.py', 'phase_c_replay_apply_20260530.py',
    'phase_c_dedup_replay_20260530.py', 'ledger_dryrun_20260530.py',
    'replay_history_dry_run.py', 'replay_history_apply.py',
    'load_purchase_history_20250529.py', 'backfill_nonstock_2026_08.py',
    'fix_k11_1155_unit_conversion.py', 'apply_stock_and_mapping_csv.py',
    'reimport_2026_04_28/import_credit_notes.py', 'reimport_2026_04_28/run.py',
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
            cols = {m.group(1).lower() for m in UPDATE_RE.finditer(flat)
                    if _relevant(flat, m.group(0))} - EXEMPT_COLS
            dels = any(_relevant(flat, m.group(0)) for m in DELETE_RE.finditer(flat))
            if cols or dels:
                rel = os.path.relpath(path, root).replace(os.sep, '/')
                out[rel] = {'update_cols': sorted(cols), 'deletes': dels}
    return out


def test_every_app_writer_is_accounted_for():
    found = _scan(APP)
    # CONTROL: a broken regex or walk empties `found` and every assertion below
    # passes vacuously — the shape this repo keeps re-learning.
    assert found, 'the sweep found no writers at all — the sweep is broken'
    assert 'models/imports.py' in found, found      # the importer must always appear
    undeclared = {f: v for f, v in found.items() if f not in DECLARED}
    assert not undeclared, (
        'these write or delete source-document rows and are not in DECLARED. '
        'Under mig 173 an undeclared UPDATE aborts, and an unstamped DELETE is '
        f'attributed to whoever last wrote the row: {undeclared}')


def test_every_script_writer_is_either_converted_or_knowingly_exempt():
    found = _scan(SCRIPTS)
    assert found, 'the script sweep found nothing — it is broken'
    unknown = sorted(set(found) - SCRIPT_EXEMPTIONS)
    assert not unknown, (
        'new or unlisted scripts write source-document rows. A dated one-off can '
        f'join SCRIPT_EXEMPTIONS; a real tool must declare instead: {unknown}')


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
