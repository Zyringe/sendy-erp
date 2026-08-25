"""Every writer of the two source-document tables must declare itself.

WHY THIS FILE EXISTS
    mig 173 makes an undeclared UPDATE of a meaningful column abort. That turns
    "did I find every writer?" from a tidiness question into a runtime failure —
    and I got it wrong twice. A hand grep missed `repoint_bsn_code`
    (/mapping/split-save would have aborted for an admin doing a split) and then
    missed `learn_acronyms_normalize` (/unit-conversions, same). Both were found
    by review, not by me re-reading my own list.

    So the sweep is mechanical and lives here, in the shape
    `.claude/rules/erp-engineering-discipline.md` prescribes: enumerate every
    site, and require a WRITTEN REASON per exemption rather than a silent pass.
    A new writer added later fails this test instead of failing in production.
"""
import os
import re

APP = os.path.join(os.path.dirname(__file__), '..', 'inventory_app')
TABLES = ('sales_transactions', 'purchase_transactions')

# Columns mig 173 does NOT guard. An UPDATE touching only these needs nothing.
EXEMPT = {'synced_to_stock', 'batch_id', 'created_at',
          'change_source', 'change_actor', 'change_reason', 'change_token'}

# Sites that DO write a guarded column, each with how it declares itself.
# ⛔ A bare string is not a reason. Say which route reaches it and what it sets.
DECLARED = {
    'models/imports.py':
        'the importer: INSERTs stamp change_source=import + the batch token; the '
        'replace path is DELETE+INSERT, which the UPDATE guard never sees',
    'models/mapping.py':
        'resolve_pending_mappings and repoint_bsn_code both go through '
        '_shared.declared_update (actor=mapping-resolve / repoint-bsn-code); '
        'the second is reachable from /mapping/split-save',
    'models/bsn_sync.py':
        'learn_acronyms_normalize bulk-rewrites `unit` from /unit-conversions and '
        'declares inline (actor=learn-acronyms); every other write here touches '
        'synced_to_stock only',
    'models/reconcile.py':
        'apply_reconcile_flag stamps the human resolver onto the rows before '
        'deleting them, so the DELETE audit row is not attributed to the importer',
    'import_credit_notes.py':
        'the ref_invoice backfill run while importing a credit-note file, which '
        'declares actor=credit-notes-backfill on the row it touches',
    'database.py':
        'the one-shot doc_base backfill inside init_db, for a DB old enough to '
        'lack the column; declares change_source=import inline',
    'models/_shared.py':
        'declared_update / declared_delete themselves — this is the mechanism',
}

WRITE = re.compile(
    r"UPDATE\s+(?:\{table\}|\{t\}|sales_transactions|purchase_transactions)\s+SET\s+"
    r"([a-z_]+)", re.I)


def _app_files():
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'instance', 'templates')]
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(root, f)


def _rel(path):
    return os.path.relpath(path, APP).replace(os.sep, '/')


def test_every_guarded_writer_is_accounted_for():
    found = {}
    for path in _app_files():
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        if not any(t in src or '{table}' in src or '{t}' in src for t in TABLES):
            continue
        if not any(t in src for t in TABLES):
            continue
        for col in WRITE.findall(src):
            if col.lower() not in EXEMPT:
                found.setdefault(_rel(path), set()).add(col.lower())

    # CONTROL: the sweep must be capable of finding anything at all. If the regex
    # or the walk breaks, `found` goes empty and every assertion below passes
    # vacuously — the exact shape this repo keeps re-learning.
    #
    # ⚠ Only the sites that still issue a RAW `UPDATE <table> SET` appear here.
    # imports.py and mapping.py are absent BECAUSE they were converted — mapping
    # now goes through declared_update and imports only ever INSERTs. Pinning the
    # expected set means a conversion that silently regresses to a raw UPDATE
    # shows up as an unexpected member, not as a quiet pass.
    assert found, 'the sweep found no guarded writes at all — the sweep is broken'
    assert set(found) == {'database.py', 'models/bsn_sync.py'}, (
        'the set of raw guarded writers moved; each one needs a decision: '
        f'{ {k: sorted(v) for k, v in found.items()} }')

    undeclared = {f: sorted(c) for f, c in found.items() if f not in DECLARED}
    assert not undeclared, (
        'these write a guarded column but are not in DECLARED — they will abort '
        f'at runtime under mig 173: {undeclared}')


def test_every_exemption_carries_a_real_reason():
    """An allowlist entry is a decision on the record. `'legacy'` is not one."""
    for site, reason in DECLARED.items():
        assert len(reason.split()) >= 8, f'{site}: reason is too thin to be one: {reason!r}'
        assert not reason.strip().lower().startswith(('todo', 'fix', 'legacy')), site


def test_the_sweep_would_catch_a_new_undeclared_writer(tmp_path, monkeypatch):
    """break-it-once, in-process: point the walk at a file that writes a guarded
    column from a module nobody declared, and the sweep must fail."""
    rogue = tmp_path / 'rogue.py'
    rogue.write_text(
        "conn.execute(\"UPDATE sales_transactions SET customer_code=? WHERE id=?\")\n",
        encoding='utf-8')
    monkeypatch.setattr('tests.test_source_doc_writer_coverage.APP', str(tmp_path))
    import tests.test_source_doc_writer_coverage as mod
    with __import__('pytest').raises(AssertionError) as e:
        mod.test_every_guarded_writer_is_accounted_for()
    assert 'customer_code' in str(e.value) or 'sweep is broken' not in str(e.value)
