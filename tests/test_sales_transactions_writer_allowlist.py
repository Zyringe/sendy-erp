"""Every INSERT/DELETE against sales_transactions ROWS is either the import
path or carries a written exemption.

Why this exists (reconcile-scan-plan.md §2): the detection universe is "ALL
Sendy sales docs in the window — no provenance filter", which is only safe
because sales_transactions holds ONLY the BSN5657 book (CSV weekly and DBF
daily import through models/imports.py::import_weekly — two transports of
the same book, no other writer). If a future change let some OTHER code
path create or delete rows here, reconcile-scan would silently start
comparing non-Express-sourced docs against the Express extract and
misclassify them as 'deleted'.

Mirrors the allowlist style of test_revenue_filter_coverage.py — a
mechanical sweep, not a read-file-by-file review (that style is what missed
two real revenue surfaces in migration 142, see that file's own docstring).

Scope: this checks row-CREATING/REMOVING statements only (INSERT/DELETE),
not UPDATE. An UPDATE that only patches an existing row (product_id remap
on mapping-fix, synced_to_stock flip, ref_invoice patch on a credit-note
link) does not add or remove a doc from the book, so it does not violate
the "sales_transactions == the BSN5657 book" premise this sweep protects.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'inventory_app')

# The one true import path — never needs a reason.
_IMPORT_PATH = 'models/imports.py'

# Files allowed to INSERT/DELETE sales_transactions rows OUTSIDE the import
# path. Each entry must say why — a reason is what makes this a decision
# rather than an oversight.
ALLOWED = {
    'models/bsn_sync.py':
        'dismiss_pending_unit_conversion() deletes only UNSYNCED rows '
        '(synced_to_stock=0, never touched stock) when a team member '
        'corrects a wrong unit entry mid-mapping — a data-entry fix on rows '
        'that never left an import batch, not a create/delete of real '
        'BSN-sourced sales history.',
    'models/reconcile.py':
        'Confirm-apply for a doc PROVEN deleted in Express (reconcile-scan-'
        'plan.md §2): apply_reconcile_flag() deletes a doc\'s rows only '
        'after the class gate, platform-customer refusal, field-wise CAS, '
        'and both-direction ledger verification all pass — companion to '
        'the import path (reconciling Sendy TO match Express), not an '
        'independent writer.',
    'import_credit_notes.py':
        'False positive from its own module docstring, which explicitly '
        'says "Do NOT insert into sales_transactions" while explaining why '
        'a new SR is recorded in credit_note_imports instead — there is no '
        'actual INSERT/DELETE statement against sales_transactions in this '
        'file (verified 2026-08-04, reconcile-scan branch).',
}

# Matches `INSERT INTO sales_transactions`, `INSERT INTO {table}`,
# `DELETE FROM sales_transactions`, `DELETE FROM {table}` — the dynamic
# `{table}` form is how models/imports.py and models/bsn_sync.py share one
# function body across sales_transactions/purchase_transactions.
_WRITE_RE = re.compile(
    r'(INSERT\s+(?:OR\s+\w+\s+)?INTO|DELETE\s+FROM)\s*["\']?\{?(sales_transactions|table)\}?',
    re.IGNORECASE)


def _py_files():
    for root, _dirs, names in os.walk(APP):
        if any(part in root for part in ('__pycache__', 'instance', 'static')):
            continue
        for n in names:
            if n.endswith('.py'):
                path = os.path.join(root, n)
                yield os.path.relpath(path, APP).replace(os.sep, '/'), path


def _writer_files():
    """Files with an INSERT/DELETE statement that touches sales_transactions
    (directly or via the {table} f-string pattern), keyed by relative path
    -> source text."""
    out = {}
    for rel, path in _py_files():
        src = open(path, encoding='utf-8').read()
        if 'sales_transactions' not in src:
            continue
        if _WRITE_RE.search(src):
            out[rel] = src
    return out


def test_no_unallowed_sales_transactions_row_writer():
    writers = set(_writer_files())
    unexpected = writers - {_IMPORT_PATH} - set(ALLOWED)
    assert not unexpected, (
        "These files INSERT/DELETE sales_transactions rows with no allowlist "
        "entry and are not the import path — wire them through models/imports.py, "
        "or add an entry to ALLOWED saying why they are exempt:\n  "
        + "\n  ".join(sorted(unexpected)))


def test_allowlist_entries_still_write():
    """A stale allowlist entry hides a surface that has since stopped writing
    (or been deleted) — the next real new writer then looks pre-approved."""
    stale = []
    for rel in ALLOWED:
        path = os.path.join(APP, rel)
        if not os.path.exists(path):
            stale.append(f'{rel} (file no longer exists)')
            continue
        src = open(path, encoding='utf-8').read()
        if 'sales_transactions' not in src or not _WRITE_RE.search(src):
            stale.append(f'{rel} (no longer writes sales_transactions rows)')
    assert not stale, "Remove these stale allowlist entries:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize('rel', sorted(ALLOWED))
def test_every_exemption_carries_a_reason(rel):
    assert len(ALLOWED[rel]) > 40, f'{rel}: explain WHY it is exempt, in a sentence'


def test_import_path_is_a_writer():
    """Positive control — the sweep would be worthless if it could pass while
    the ONE path it exists to allow silently lost its own INSERT/DELETE."""
    src = open(os.path.join(APP, _IMPORT_PATH), encoding='utf-8').read()
    assert _WRITE_RE.search(src), f'{_IMPORT_PATH} no longer writes sales_transactions rows'
