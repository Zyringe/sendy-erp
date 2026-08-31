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
import io
import os
import re
import tokenize

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
    'models/payments.py':
        'AR-balance surfaces, not revenue reports. get_payment_status and '
        'get_payment_summary are the document ledger behind /ar?tab=รายบิล — '
        'same stance as models/sales.py, every document that exists must show. '
        'get_ar_reconciliation is deliberately UNFILTERED: it IS the '
        '"Ledger (Sendy)" column that /ar?tab=กระทบยอด holds up against the '
        'filtered Express snapshot, so guarding it would make the comparison '
        'compare nothing. find_payment_candidates carries a STRICTER guard of '
        'its own — the whole ar_writeoffs table rather than the '
        'revenue-only subset — because it recommends where incoming cash '
        'belongs (2026-08-31).',
    'models/marketplace.py':
        'set_amount_review sums one marketplace order\'s own billed lines to '
        'compare against that order\'s payout — a per-order figure for IV '
        'matching, never aggregated into a revenue total.',
    'models/pricing_ap.py':
        'get_product_pricing_summary / get_product_pricing compute a '
        'per-product realised selling price from sales history, not a revenue '
        'report. ⚠ OPEN, measured 2026-08-31: the three written-off giveaway '
        'documents put 36 lines / ฿154,122.80 across 36 products into that '
        'history, so those products\' realised price includes goods that were '
        'given away. Whether price evidence should exclude write-offs is a '
        'pricing call for Put, not a revenue-guard question — tracked apart.',
}
# NOTE: models/accounting.py, models/customers.py and models/financial_health.py
# are NOT listed — they carry the guard. The one per-query nuance (accounting's
# COGS query left unfiltered on purpose) is pinned by
# test_giveaway_revenue_exclusion.py, which asserts on actual numbers rather
# than on the presence of a token. All four revenue surfaces now report the
# same figure; that agreement is itself a test.

GUARD_TOKENS = ('excludes_revenue', 'not_a_sale_clause', 'revenue_filter',
                '_SALES_FILTER')

# Two shapes, because the first version of this regex only knew the bare one and
# was therefore blind to every VAT-aware AR query in the app — the whole of
# models/payments.py (10 aggregates, 0 of them bare) sat outside the sweep and
# outside the allowlist, and a /scrutinize pass on 2026-08-31 found it there
# proposing accountant-written-off invoices as the owner of incoming cash.
# `.{0,200}?` rather than `[^()]*?` so an inner call — ROUND(net, 2) — still
# counts. Over-inclusive on purpose: a false hit costs one allowlist decision,
# a false miss costs a wrong number on a money page.
_SUM_NET = re.compile(
    r'SUM\(\s*(?:(?:[a-z]{1,3}\.)?net\s*\)|CASE\b.{0,200}?\bnet\b)',
    re.IGNORECASE | re.DOTALL)


def _code_only(src):
    """`src` with comments and docstrings removed.

    A guard token found in prose is not a guard. Writing the words
    "excludes_revenue = 1" into a docstring — which is exactly what the
    2026-08-31 fix to models/payments.py did, while explaining that it uses a
    DIFFERENT filter — silently marked the whole file as participating.
    """
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.lstrip().startswith(('"""', "'''")):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src          # unparseable: fall back to the raw text, never skip
    return '\n'.join(out)


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
        if any(tok in _code_only(src) for tok in GUARD_TOKENS):
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
        assert any(tok in _code_only(src) for tok in GUARD_TOKENS), \
            f'{rel} lost its revenue guard'


# ── the sweep's own coverage ─────────────────────────────────────────────────
#
# A sweep is only worth what its pattern can see, and nothing about reading it
# reveals a shape it misses. Each entry below is a shape that exists in this
# app; each NOT_ entry is one that must stay invisible or the sweep flags the
# whole codebase and gets deleted for noise.

AGGREGATE_SHAPES = {
    'bare':              'SELECT SUM(net) FROM sales_transactions',
    'aliased':           'SELECT SUM(st.net) FROM sales_transactions st',
    'vat_case':          'SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)',
    'vat_case_aliased':  'SUM(CASE WHEN st.vat_type=2 THEN st.net*1.07 ELSE st.net END)',
    'rounded_vat_case':  'ROUND(SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END), 2)',
    'case_inner_call':   'SUM(CASE WHEN x THEN ROUND(net, 2) ELSE 0 END)',
    'case_over_lines':   'SUM(CASE WHEN vat_type = 2\n THEN net * 1.07\n ELSE net END)',
}

NOT_AGGREGATES = {
    'bare column':        'SELECT net FROM sales_transactions',
    'different column':   'SELECT SUM(qty) FROM sales_transactions',
    'net inside a name':  'SELECT SUM(total_net_x) FROM t',
    'case without net':   'SUM(CASE WHEN vat_type = 2 THEN qty ELSE 0 END)',
}


@pytest.mark.parametrize('shape', sorted(AGGREGATE_SHAPES))
def test_the_sweep_sees_every_aggregate_shape(shape):
    assert _SUM_NET.search(AGGREGATE_SHAPES[shape]), \
        f'{shape}: the sweep is blind to this shape, so any file using it is unswept'


@pytest.mark.parametrize('shape', sorted(NOT_AGGREGATES))
def test_the_sweep_ignores_what_is_not_a_net_aggregate(shape):
    assert not _SUM_NET.search(NOT_AGGREGATES[shape]), f'{shape}: false positive'


def test_a_guard_token_only_in_prose_does_not_count_as_a_guard():
    """Otherwise a file can be marked guarded by a comment that says the
    opposite — how models/payments.py briefly passed on 2026-08-31."""
    prose_only = (
        '"""We deliberately do NOT use excludes_revenue here."""\n'
        '# revenue_filter is the wrong population for this query\n'
        'x = 1\n'
    )
    assert not any(tok in _code_only(prose_only) for tok in GUARD_TOKENS)
    # CONTROL: the same token in real code still counts, or the strip ate
    # everything and every file would read as unguarded.
    real = 'from sales_filters import revenue_filter\nq = revenue_filter()\n'
    assert any(tok in _code_only(real) for tok in GUARD_TOKENS)
