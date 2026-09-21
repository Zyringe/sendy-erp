"""Every COGS reader is on ONE basis, and each exemption carries a reason.

`.claude/rules/erp-engineering-discipline.md`: when a change redefines a shared
concept, grep the whole app for it FIRST, decide each site explicitly, and
leave the sweep behind as a test whose allowlist demands a written reason. The
concept here is "what does one unit of a sold thing cost" — it moved from
`products.cost_price` (today's average) to sales_filters.cogs_unit_cost_sql()
(ADR 0015). A site left behind keeps drifting after the headline stopped, and
the drift is invisible: both numbers look plausible.

The sweep keys on base_qty_sql(), which is the app's one marker for "this
query costs sales lines in base units". A file that converts a bill's qty to
base units and multiplies by a cost is a COGS reader by definition.

⚠ What this cannot see: a COGS computed without base_qty_sql() (marketplace.py
carries its own per-listing ratio), SQL assembled at runtime, and anything
outside `inventory_app/`. Those are judged by hand and listed below.
"""
import os
import re

import pytest

import sales_filters


APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# path -> why this file costs sales lines WITHOUT the shared basis.
EXEMPT = {
    'models/marketplace.py':
        'Per-ORDER settlement COGS, not a period statement: it carries its own '
        'per-listing qty_per_sale ratio and never calls base_qty_sql(), so the '
        'sweep does not reach it. Answers "what did this order earn", which is '
        'asked at analysis time about a marketplace payout, not "what did this '
        'month cost". Left on cost_price deliberately.',
}


def _cogs_readers():
    """Files that convert a bill qty to base units, i.e. cost sales lines."""
    hits = []
    for root, _dirs, files in os.walk(APP):
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, APP)
            if rel == 'sales_filters.py':
                continue
            with open(path, encoding='utf-8') as fh:
                src = fh.read()
            if 'base_qty_sql' in src:
                hits.append((rel, src))
    return hits


def test_the_sweep_finds_something():
    """Control. A sweep that matched nothing would pass every assertion below
    while proving nothing at all."""
    assert len(_cogs_readers()) >= 2


def test_every_cogs_reader_is_on_the_shared_basis():
    offenders = []
    for rel, src in _cogs_readers():
        if rel in EXEMPT:
            continue
        if 'cogs_unit_cost_sql' not in src:
            offenders.append(rel)
    assert not offenders, (
        "these cost sales lines in base units but not through "
        "sales_filters.cogs_unit_cost_sql(): %s. Either move them onto it, or "
        "add them to EXEMPT with a written reason." % offenders)


def test_no_cogs_reader_still_multiplies_raw_cost_price():
    """The shape the change replaced: `base_qty * COALESCE(p.cost_price, 0)`.
    Catches a site that imports the helper for one query and hand-writes
    another, which no file-level check would see."""
    pattern = re.compile(r'base_qty_sql\(\)[^;]{0,200}?COALESCE\(\s*p\.cost_price',
                         re.DOTALL)
    offenders = [rel for rel, src in _cogs_readers()
                 if rel not in EXEMPT and pattern.search(src)]
    assert not offenders, (
        "raw cost_price is still multiplied by a base qty in: %s" % offenders)


def test_every_exemption_names_a_real_file():
    """An exemption for a file that has moved is a silent hole."""
    for rel in EXEMPT:
        assert os.path.exists(os.path.join(APP, rel)), rel


def test_the_cutover_matches_the_ledgers_own_reset_date():
    """`COGS_HISTORICAL_FROM` is only correct because `wacc.py` resets every
    product's cost on exactly that date. Two literals in two modules drift
    silently, and the seam breaks without any test failing on its own."""
    import models.wacc as wacc
    assert sales_filters.COGS_HISTORICAL_FROM == wacc._WACC_INITIAL_DATE


def test_the_expression_reuses_the_apps_own_latest_cost_ordering():
    """`wacc.get_current_wacc` answers "the newest cost" with the identical
    ORDER BY. If either side changes alone the P&L and the product page start
    telling different stories about the same product."""
    import inspect

    import models.wacc as wacc
    ordering = 'ORDER BY event_date DESC, id DESC LIMIT 1'
    body = inspect.getsource(wacc.get_current_wacc)
    assert ordering in body, 'wacc.get_current_wacc no longer uses %r' % ordering
    assert ('ORDER BY pcl.event_date DESC, pcl.id DESC LIMIT 1'
            in sales_filters.cogs_unit_cost_sql())
