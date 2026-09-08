"""Query-param paging is parsed in ONE place (inventory_app/paging.py).

Card 7 of the 2026-09-08 architecture review: 13 blueprint sites each
re-derived `int(request.args.get('page', 1))` in one of three idioms, and
the bare-int ones raise ValueError -> 500 on a typo'd URL (?page=abc).

`accounting.py` had already been fixed once, in place, with a comment
describing this exact failure. That single-route fix is the semantic this
module generalises: **anything that is not a usable page number resolves
to page 1** — non-numeric, zero, negative, or large enough to overflow the
SQLite OFFSET bind.

Three layers here:
  1. unit    — the parser itself (paging.page_arg / paging.paging)
  2. route   — all 13 real pages survive ?page=abc and ?page=<huge>
  3. sweep   — no blueprint re-implements the parse (allowlist demands a reason)
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / 'inventory_app'

# (review-report site, url template). The report measured 13; if this list
# ever disagrees with the sweep test below, one of them is stale.
PAGING_SITES = [
    ('sales.py:46',       '/sales'),
    ('sales.py:102',      '/purchases'),
    ('products.py:154',   '/products'),
    ('bsn.py:93',         '/unit-conversions'),
    ('ecommerce.py:39',   '/ecommerce'),
    ('inventory.py:144',  '/transactions'),
    ('partners.py:29',    '/customers'),
    ('partners.py:194',   '/customers/bulk-reassign'),
    ('partners.py:223',   '/suppliers'),
    ('labels.py:88',      '/labels/manage'),
    ('naming.py:157',     '/naming'),
    ('cashbook.py:571',   '/cashbook/account/{account_id}'),
    ('accounting.py:352', '/ar?tab=invoices'),
]

# A page number that parses fine in Python but cannot bind to a SQLite
# INTEGER — the overflow accounting.py's comment describes.
HUGE_PAGE = '9' * 25


@pytest.fixture
def admin_client(tmp_db):
    """Admin-session test client over a live-DB copy (idiom from test_bp_products_routes)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


@pytest.fixture
def account_id(tmp_db):
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM cashbook_accounts ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No cashbook_accounts in live DB clone")
    return row[0]


def _url(template, account_id, **params):
    url = template.format(account_id=account_id)
    sep = '&' if '?' in url else '?'
    return url + sep + '&'.join(f'{k}={v}' for k, v in params.items())


# ── 1. the parser itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('3',      3),      # ordinary
    ('1',      1),
    ('abc',    1),      # the 500 this card exists to kill
    ('',       1),
    ('0',      1),      # zero is not a page
    ('-5',     1),      # negative offset
    ('2.5',    1),      # float string: int() would raise
    (HUGE_PAGE, 1),     # overflows the SQLite OFFSET bind
])
def test_page_arg_resolves_unusable_values_to_one(raw, expected):
    from paging import page_arg
    assert page_arg({'page': raw}) == expected


def test_page_arg_defaults_to_one_when_absent():
    from paging import page_arg
    assert page_arg({}) == 1


def test_paging_returns_per_page_from_app_config(tmp_db):
    """Default per_page is the app's ITEMS_PER_PAGE, not a literal."""
    from app import app as flask_app
    from paging import paging
    with flask_app.test_request_context('/products?page=4'):
        from flask import request
        page, per_page = paging(request.args)
    assert page == 4
    assert per_page == flask_app.config['ITEMS_PER_PAGE']


def test_paging_honours_an_explicit_per_page(tmp_db):
    """Sites with their own page size (cashbook 50, bulk-reassign 100) pass it in."""
    from app import app as flask_app
    from paging import paging
    with flask_app.test_request_context('/x?page=abc'):
        from flask import request
        page, per_page = paging(request.args, per_page=100)
    assert (page, per_page) == (1, 100)


# ── 2. every real page survives a typo'd URL ─────────────────────────────────

def test_all_thirteen_reported_sites_are_listed():
    """Guard the parametrize lists below against silently shrinking to nothing."""
    assert len(PAGING_SITES) == 13


@pytest.mark.parametrize('site, template', PAGING_SITES)
def test_page_one_renders(admin_client, account_id, site, template):
    """CONTROL: the route is reachable at all for this client, so a failure
    in the ?page=abc test below means the parse, not a broken fixture."""
    resp = admin_client.get(_url(template, account_id, page=1))
    assert resp.status_code == 200, f'{site}: {resp.status_code} {resp.data[:300]}'


@pytest.mark.parametrize('site, template', PAGING_SITES)
def test_non_numeric_page_does_not_500(admin_client, account_id, site, template):
    resp = admin_client.get(_url(template, account_id, page='abc'))
    assert resp.status_code == 200, f'{site}: {resp.status_code} {resp.data[:300]}'


@pytest.mark.parametrize('site, template', PAGING_SITES)
def test_overflowing_page_does_not_500(admin_client, account_id, site, template):
    resp = admin_client.get(_url(template, account_id, page=HUGE_PAGE))
    assert resp.status_code == 200, f'{site}: {resp.status_code} {resp.data[:300]}'


# ── 3. nobody re-implements the parse ────────────────────────────────────────

# Matches any read of ?page / ?per_page straight off request.args, in every
# idiom the review found (bare int, max(1, int(...)), type=int), either quote.
#
# What this sweep CANNOT see, by construction:
#   - subscript access, `request.args['page']`
#   - a name bound first, `a = request.args; a.get('page')`
#   - paging parsed in a template, or in JS that builds the URL
#   - non-.py sources
# The route tests above are the real net for those: they drive the actual page.
_RAW_PARSE = re.compile(r"""args\.get\(\s*['"](page|per_page)['"]""")

# relative path -> why that file is allowed to parse paging itself.
# A new entry needs a written reason, not just a path.
ALLOWED_RAW_PARSE = {
    'paging.py': "it IS the definition — plus the three old idioms quoted in its docstring",
}


def test_nothing_re_derives_paging():
    files = sorted(APP_DIR.rglob('*.py'))
    assert len(files) > 40, f'sweep found only {len(files)} files — it never ran'

    offenders = []
    for f in files:
        rel = f.relative_to(APP_DIR).as_posix()
        for i, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            if _RAW_PARSE.search(line) and rel not in ALLOWED_RAW_PARSE:
                offenders.append(f'{rel}:{i}: {line.strip()}')
    assert not offenders, (
        'These sites parse paging themselves instead of calling paging.py.\n'
        'Use `page, per_page = paging(request.args)`, or add the file to\n'
        'ALLOWED_RAW_PARSE with a written reason.\n  ' + '\n  '.join(offenders)
    )


def test_sweep_can_actually_see_the_pattern():
    """CONTROL for the sweep: a known-offending line must be flagged."""
    assert _RAW_PARSE.search("    page = int(request.args.get('page', 1))")
    assert _RAW_PARSE.search('    page = request.args.get("page", 1, type=int) or 1')
    assert not _RAW_PARSE.search("    q = request.args.get('q', '').strip()")
