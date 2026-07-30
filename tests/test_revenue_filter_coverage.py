"""Every place that sums sales_transactions.net is either guarded or listed here.

Why this exists: the giveaway-exclusion change (migration 142) was first shipped
having patched only the surfaces its author happened to think of. A /scrutinize
sweep then found two that were missed — and both mattered:

  models/customers.py  the customer LIST page still counted the giveaway while
                       the DETAIL page didn't: ฿499,577.31 vs ฿345,454.51 for
                       วรสวัสดิ์, one click apart.
  commission.py        the giveaway carried salesperson 02 and ฿154,122.80 of
                       net straight into the payable-commission base — real
                       money, for goods given away free.

Reading code file-by-file is what missed them. This test does the sweep
mechanically, so the next person who adds a revenue surface (or forgets one)
gets told at CI time instead of by a wrong number on a page.

The allowlist below is the record of DELIBERATE exceptions. Adding an entry is
fine — silently leaving a surface unguarded is not.
"""
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# Files that sum `net` off sales_transactions WITHOUT the revenue guard, on
# purpose. Each entry must say why — a reason is what makes this a decision
# rather than an oversight.
ALLOWED = {
    'models/sales.py':
        'Document ledger — /sales mirrors Express and must show every document '
        'that exists, including ones invoiced in error. Its totals therefore '
        'differ from /accounting by design (Put, 2026-07-30, option ก).',
    'blueprints/mobile.py':
        'Mobile view of the same document ledger as models/sales.py — same call.',
    'payments_alloc.py':
        'Sums SR (credit-note) rows for AR allocation, not revenue.',
    'call_card.py':
        'Sales-rep call card shows a customer purchase history, not a revenue '
        'report; left with the ledger for consistency with /sales.',
}
# NOTE: models/accounting.py, models/customers.py and models/financial_health.py
# are NOT listed — they carry the guard. The one per-query nuance (accounting's
# COGS query left unfiltered on purpose) is pinned by
# test_giveaway_revenue_exclusion.py, which asserts on actual numbers rather
# than on the presence of a token. All four revenue surfaces now report the
# same figure; that agreement is itself a test.

GUARD_TOKENS = ('excludes_revenue', 'not_a_sale_clause', 'revenue_filter',
                '_SALES_FILTER')
_SUM_NET = re.compile(r'SUM\(\s*(?:[a-z]{1,3}\.)?net\s*\)', re.IGNORECASE)


def _py_files():
    for root, _dirs, names in os.walk(APP):
        if any(part in root for part in ('__pycache__', 'instance', 'static')):
            continue
        for n in names:
            if n.endswith('.py'):
                path = os.path.join(root, n)
                yield os.path.relpath(path, APP).replace(os.sep, '/'), path


def _unguarded_files():
    """Files with a SUM(net) over sales_transactions and no guard token."""
    out = {}
    for rel, path in _py_files():
        src = open(path, encoding='utf-8').read()
        if 'sales_transactions' not in src or not _SUM_NET.search(src):
            continue
        if any(tok in src for tok in GUARD_TOKENS):
            # File participates in the guard. Per-query correctness is covered
            # by the reconciliation tests; this sweep is about whole surfaces
            # nobody wired up at all.
            continue
        out[rel] = src
    return out


def test_no_unguarded_revenue_surface_outside_the_allowlist():
    unguarded = set(_unguarded_files())
    unexpected = unguarded - set(ALLOWED)
    assert not unexpected, (
        "These files sum sales_transactions.net with no revenue guard and no "
        "allowlist entry — wire them to sales_filters, or add an entry saying "
        "why they are exempt:\n  " + "\n  ".join(sorted(unexpected)))


def test_allowlist_entries_still_apply():
    """A stale allowlist is worse than none — it hides a surface that has since
    been wired up (or deleted), so the next real miss looks intentional."""
    stale = []
    for rel in ALLOWED:
        path = os.path.join(APP, rel)
        if not os.path.exists(path):
            stale.append(f'{rel} (file no longer exists)')
            continue
        src = open(path, encoding='utf-8').read()
        if 'sales_transactions' not in src or not _SUM_NET.search(src):
            stale.append(f'{rel} (no longer sums net off sales_transactions)')
    assert not stale, "Remove these stale allowlist entries:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize('rel', sorted(ALLOWED))
def test_every_exemption_carries_a_reason(rel):
    assert len(ALLOWED[rel]) > 40, f'{rel}: explain WHY it is exempt, in a sentence'


def test_the_surfaces_that_must_be_guarded_are():
    """Positive control — the sweep would be worthless if it could pass while
    the pages it exists to protect quietly lost their guard."""
    must_guard = ('revenue.py', 'cashflow.py', 'models/accounting.py',
                  'models/customers.py', 'commission.py')
    for rel in must_guard:
        src = open(os.path.join(APP, rel), encoding='utf-8').read()
        assert any(tok in src for tok in GUARD_TOKENS), \
            f'{rel} lost its revenue guard'
