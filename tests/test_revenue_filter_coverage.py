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
        'report. Its /call spend is ยอดซื้อรวม (#494: credit notes subtracted, '
        'HS kept, only the not-a-sale guard); the product lists stay on the '
        'ledger for consistency with /sales.',
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
    r'SUM\(\s*(?:(?:[a-z]{1,3}\.)?net\s*\)|CASE\b.{0,200}?\bnet\b'
    # The net→cash CASE moved into vat_math (2026-09-09, card 2). A call site
    # now reads SUM({vat_math.cash_sql()}) and carries no literal `net`, so the
    # pattern above would stop seeing a revenue surface that is still there.
    r'|\{vat_math\.cash_sql\()',
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


# ── #514: HS is a cash sale, not an opening balance — sweep the leftovers ───
#
# sales_filters.revenue_filter() dropped its `NOT LIKE 'HS%'` clause (#514):
# an HS document is a cash sale, real revenue, real price evidence. A file
# that still hand-types its OWN `NOT LIKE 'HS%'` exclusion is either a
# leftover that needs the same fix, or a deliberate AR/settlement/stock-
# quantity site that keeps excluding HS on purpose — the issue names
# `ar_diagnostic` / `_settlement_rows` as the deliberate ones. Same shape as
# the ALLOWED sweep above: silently leaving one unswept is the failure mode.
HS_EXCLUSION_ALLOWED = {
    'models/payments.py':
        'AR-balance surfaces (payment-status, unpaid bills, customer debt) — '
        'HS is paid on the spot, never a receivable (#514).',
    'payments_alloc.py':
        'Invoice settlement / cash allocation — the issue names '
        '_settlement_rows explicitly as keep-excluded (#514).',
    'blueprints/mobile.py':
        "/m/sales-trip's per-customer 'outstanding' figure is an AR figure, "
        'same reason as models/payments.py (#514).',
    'models/ecommerce_overview.py':
        'Marketplace STOCK-quantity deduction (sold units reducing the '
        'platform stock estimate), not revenue or price evidence — a '
        'separate business question, deliberately left unchanged pending '
        "Put's call (#514).",
}

# Matched against the RAW source, never `_code_only(src)`. `_code_only` strips
# any string token whose token-line, lstripped, starts with `"""`/`'''` — a
# heuristic aimed at real docstrings that also fires on a SQL string built as
# the first argument to `cur.execute(` (financial_health.py's own trailing-
# months query is exactly this shape). That silently hid an injected
# `NOT LIKE 'HS%'` from this sweep in 14 files across the app (proved below
# by re-injecting one into financial_health.py and watching the sweep miss
# it before this fix, and catch it after). Comments and docstrings are NOT
# stripped for this sweep on purpose: a legitimate comment that happens to
# mention the pattern is an allowlist entry with a reason, not grounds to go
# back to stripping.
#
# The pattern tolerates the noise Python string-building puts between the
# keywords (quotes, `+`, whitespace/newlines from literal concatenation) so
# it survives the shapes below without needing one alternative per shape:
#   NOT.{0,5}?LIKE.{0,10}?HS%   -- any `... NOT ... LIKE ... 'HS%'`-ish clause
#   <>.{0,10}?HS['"]            -- substr(doc_no,1,2) <> 'HS' (no % wildcard)
_HS_EXCLUSION_RE = re.compile(
    r"NOT.{0,5}?LIKE.{0,10}?HS%"
    r"|<>.{0,10}?HS['\"]",
    re.IGNORECASE | re.DOTALL)


def test_no_stray_hs_exclusion_outside_the_allowlist():
    """After #514, a hand-typed HS exclusion may only survive in a file
    listed above with a reason. Every revenue/price surface must have
    dropped it (either by importing sales_filters.revenue_filter, or by
    dropping its own copy of the clause)."""
    hits = set()
    for rel, path in _py_files():
        src = open(path, encoding='utf-8').read()
        if _HS_EXCLUSION_RE.search(src):
            hits.add(rel)
    unexpected = hits - set(HS_EXCLUSION_ALLOWED)
    assert not unexpected, (
        "These files still hand-exclude HS (\"NOT LIKE 'HS%'\" or "
        "equivalent) with no allowlist entry — either they should now "
        "count HS as revenue (#514), or add a reason:\n  "
        + "\n  ".join(sorted(unexpected)))


@pytest.mark.parametrize('rel', sorted(HS_EXCLUSION_ALLOWED))
def test_every_hs_exemption_carries_a_reason(rel):
    assert len(HS_EXCLUSION_ALLOWED[rel]) > 40, f'{rel}: explain WHY it still excludes HS'


def test_hs_allowlist_entries_still_apply():
    """A stale entry hides a surface that has since dropped the exclusion
    (or been deleted) — same trap as the revenue-guard allowlist above."""
    stale = []
    for rel in HS_EXCLUSION_ALLOWED:
        path = os.path.join(APP, rel)
        if not os.path.exists(path):
            stale.append(f'{rel} (file no longer exists)')
            continue
        src = open(path, encoding='utf-8').read()
        if not _HS_EXCLUSION_RE.search(src):
            stale.append(f'{rel} (no longer hand-excludes HS)')
    assert not stale, "Remove these stale HS-allowlist entries:\n  " + "\n  ".join(stale)


def test_sales_filters_revenue_filter_itself_does_not_exclude_hs():
    """Positive control — the sweep above only catches files that DUPLICATE
    the clause; it says nothing about the one shared definition itself."""
    import sales_filters
    assert "NOT LIKE 'HS%'" not in sales_filters.revenue_filter()


# ── the HS sweep's own coverage ──────────────────────────────────────────────
#
# Mirrors AGGREGATE_SHAPES/NOT_AGGREGATES below for `_SUM_NET`: each entry is
# a shape the HS sweep must see (or must NOT see), so a change to the regex
# that silently narrows or widens it gets caught here instead of on a real
# file. The BLIND_BEFORE_FIX shapes are exactly what `_code_only()` matching
# missed (see the block comment above `_HS_EXCLUSION_RE`); the SEEN_BEFORE
# shapes already worked and must keep working.

HS_EXCLUSION_SHAPES_BLIND_BEFORE_FIX = {
    'string_concat':
        '"st.doc_base NOT LIKE " + "\'HS%\'"',
    'double_quoted_literal':
        'doc_base NOT LIKE "HS%"',
    'substr_not_equal':
        "substr(doc_no, 1, 2) <> 'HS'",
    'double_space_not_like':
        "doc_base NOT  LIKE 'HS%'",
    'indented_triple_quote_block':
        '            """SELECT COALESCE(SUM(net), 0) AS rev\n'
        "               FROM sales_transactions\n"
        "               WHERE doc_base NOT LIKE 'HS%'\"\"\"",
}

HS_EXCLUSION_SHAPES_SEEN_BEFORE = {
    'plain':
        "doc_base NOT LIKE 'HS%'",
    'aliased':
        "st.doc_base NOT LIKE 'HS%'",
    'f_string':
        "f\"{p}doc_base NOT LIKE 'HS%'\"",
    'format_call':
        '"{}doc_base NOT LIKE \'HS%\'".format(p)',
    'inline_triple_quote':
        'q = """SELECT 1 WHERE doc_base NOT LIKE \'HS%\'"""',
}

NOT_HS_EXCLUSIONS = {
    'sr_exclusion_not_hs':
        "doc_base NOT LIKE 'SR%'",
    'unrelated_customer_pattern':
        "customer NOT LIKE 'หน้าร้าน%'",
    'positive_hs_like_no_not':
        "doc_base LIKE 'HS%'",
    'purchase_side_prefix':
        "doc_base NOT LIKE 'GR%'",
}


@pytest.mark.parametrize('shape', sorted(HS_EXCLUSION_SHAPES_BLIND_BEFORE_FIX))
def test_the_hs_sweep_sees_shapes_that_were_blind_before_the_fix(shape):
    src = HS_EXCLUSION_SHAPES_BLIND_BEFORE_FIX[shape]
    assert _HS_EXCLUSION_RE.search(src), \
        f'{shape}: the HS sweep is blind to this shape, so a file using it is unswept'


@pytest.mark.parametrize('shape', sorted(HS_EXCLUSION_SHAPES_SEEN_BEFORE))
def test_the_hs_sweep_still_sees_shapes_it_already_saw(shape):
    src = HS_EXCLUSION_SHAPES_SEEN_BEFORE[shape]
    assert _HS_EXCLUSION_RE.search(src), \
        f'{shape}: a previously-visible shape stopped matching'


@pytest.mark.parametrize('shape', sorted(NOT_HS_EXCLUSIONS))
def test_the_hs_sweep_ignores_what_is_not_an_hs_exclusion(shape):
    assert not _HS_EXCLUSION_RE.search(NOT_HS_EXCLUSIONS[shape]), f'{shape}: false positive'


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
    'vat_math_owner':    'SUM({vat_math.cash_sql()})',
    'vat_math_aliased':  "SUM({vat_math.cash_sql('st')})",
    'vat_math_rounded':  'ROUND(SUM({vat_math.cash_sql()}), 2)',
}

NOT_AGGREGATES = {
    'bare column':        'SELECT net FROM sales_transactions',
    'different column':   'SELECT SUM(qty) FROM sales_transactions',
    'net inside a name':  'SELECT SUM(total_net_x) FROM t',
    'case without net':   'SUM(CASE WHEN vat_type = 2 THEN qty ELSE 0 END)',
    'vat_math in prose':  'see vat_math.cash_sql for the rule',
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
