"""Platform registry sweep: the marketplace code names its platforms in ONE place.

TikTok orders reach marketplace_orders from migration 197 (plan:
projects/tiktok-order-import/tiktok-order-import-plan.md, PR-1 step 5). Every
spot that still says "shopee or lazada" is a place a TikTok order silently
falls out of: a whitelist that drops it, an `if shopee else Lazada` badge that
labels it Lazada. The registry is `models._shared.PLATFORMS`.

Two checks:
  1. Every `CHECK(platform IN (...))` in the schema is a subset of PLATFORMS,
     both in the migrated DB and in the checked-in data/schema.sql.
  2. No two-platform literal remains in the marketplace modules, except the
     named exemptions below, each with its reason. An exemption is keyed by
     (file, a fragment of the offending line), so a NEW literal in the same
     file is still caught.

What this cannot see: a platform list spelled some other way (a set built in
a loop, `platform != 'tiktok'`, a default argument `platform='shopee'`). The
settlement/IV functions default to 'shopee' on purpose (PR-2/PR-3 scope) and
are not matched here. The DB CHECK is the enforcement; this only moves the
failure into CI.

PR-3 backlog: two-platform maps shaped as a dict or a SQL CASE, which neither
regex sees. Each must learn tiktok before settlement/IV linking opens to it:
  - marketplace_match._CUST_CODE            {'shopee': ..., 'lazada': ...}
  - models.marketplace._RECON_CUSTOMER      .get(platform, 'หน้าร้านS') falls back silently
  - models.marketplace._BILLED_BASIS_SQL    CASE WHEN mo.platform='lazada' ... ELSE (shopee)
  - models.marketplace.get_marketplace_order  the same billed-basis CASE, a second copy
  (and marketplace_match's own billed-basis CASE, the third copy)
"""
import os
import re

import pytest

from models._shared import PLATFORMS

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APP = os.path.join(REPO, 'inventory_app')
MIG_197 = os.path.join(REPO, 'data', 'migrations', '197_marketplace_orders_tiktok.sql')

SWEPT = [
    'blueprints/marketplace.py', 'models/marketplace.py', 'marketplace_files.py',
    'marketplace_reconcile.py', 'marketplace_match.py', 'cashbook_payout_mirror.py',
    'models/ecommerce_overview.py', 'blueprints/ecommerce.py',
    'templates/ecommerce.html', 'templates/ecommerce_product.html',
] + [os.path.join('templates', 'marketplace', f)
     for f in sorted(os.listdir(os.path.join(APP, 'templates', 'marketplace')))]

# 'shopee','lazada' with no 'tiktok' after it.
PAIR = re.compile(r"""['"]shopee['"]\s*,\s*['"]lazada['"](?!\s*,\s*['"]tiktok)""")
# A two-way branch on shopee: `== 'shopee' %}...{% else %}` or `if x == 'shopee' else`.
BINARY = re.compile(r"""==\s*'shopee'\s*(%\}[^{]*\{%-?\s*else\s*-?%\}|else\b)""")

EXEMPT = {
    ('blueprints/marketplace.py', "def review():"):
        'IV worklist (/marketplace/review) is IV linking: PR-3 adds tiktok with '
        '_CUST_CODE and the billed basis. Until then a tiktok worklist would read '
        'Shopee customer codes.',
    ('blueprints/ecommerce.py', "def ecommerce_export"):
        'Mass-update listing export: TikTok has no mass-update writer. Listing files, '
        'not orders.',
    ('blueprints/ecommerce.py', "flash('ระบุ platform ไม่ถูกต้อง'"):
        'Legacy ecommerce_listings import from order files (Shopee/Lazada parsers by '
        'hand-picked platform). Not the order import; TikTok listings arrive via '
        'platform_skus.',
    ('blueprints/marketplace.py', "def settlement():"):
        'Settlement page whitelist: TikTok settlement and IV reconcile are PR-2/PR-3. '
        "Without it ?platform=tiktok reconciled TikTok orders against Shopee IVs "
        "(_RECON_CUSTOMER.get falls back to 'หน้าร้านS').",
    ('templates/marketplace/settlement.html', "หน้าร้าน{{ 'S' if platform"):
        "Express customer code for the รับชำระหนี้ worksheet. Only reachable for "
        "shopee/lazada: settlement() whitelists the platform (exempt above) until "
        "PR-2/PR-3 opens it to tiktok.",
}


def _offending_lines(rel):
    """[(line, context)] per hit; context = the line, 12 above and 2 below (a
    whitelist sits under its def, above its flash). Located by offset, so two
    identical lines in one file each get their own context."""
    with open(os.path.join(APP, rel), encoding='utf-8') as f:
        text = f.read()
    lines = text.split('\n')
    hits = []
    for rx in (PAIR, BINARY):
        for m in rx.finditer(text):
            i = text.count('\n', 0, m.start())
            hits.append((lines[i], '\n'.join(lines[max(0, i - 12):i + 3])))
    return hits


def _exempt_key(rel, line, ctx):
    for (f, frag) in EXEMPT:
        if f == rel and (frag in line or frag in ctx):
            return (f, frag)
    return None


def test_every_platform_check_is_within_the_registry(empty_db_conn):
    with open(MIG_197, encoding='utf-8') as f:
        empty_db_conn.executescript(f.read())
    sqls = [r[0] for r in empty_db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql LIKE '%CHECK%platform IN%'")]
    with open(os.path.join(REPO, 'data', 'schema.sql'), encoding='utf-8') as f:
        sqls.append(f.read())
    checks = [set(re.findall(r"'(\w+)'", c)) for s in sqls
              for c in re.findall(r"CHECK\s*\(\s*platform\s+IN\s*\(([^)]*)\)", s)]
    # Control: platform_skus, platform_products, ecommerce_listings, marketplace_orders
    # in the DB, the same four again in schema.sql.
    assert len(checks) >= 8
    assert all(c <= set(PLATFORMS) for c in checks), checks
    assert sum('tiktok' in c for c in checks) == len(checks)


def test_no_unlisted_two_platform_literal_in_marketplace_code():
    assert len(SWEPT) >= 15, 'control: the marketplace templates were listed'
    unlisted, used = [], set()
    for rel in SWEPT:
        for line, ctx in _offending_lines(rel):
            key = _exempt_key(rel, line, ctx)
            if key:
                used.add(key)
            else:
                unlisted.append(f'{rel}: {line.strip()}')
    assert not unlisted, 'shopee/lazada-only literal (derive from PLATFORMS, or exempt with a reason):\n' \
        + '\n'.join(unlisted)
    assert set(EXEMPT) == used, f'stale exemptions: {set(EXEMPT) - used}'


@pytest.mark.parametrize('snippet', [
    "if platform in ('shopee', 'lazada'):",
    "{% if o.platform == 'shopee' %}<b>Shopee</b>{% else %}<b>Lazada</b>{% endif %}",
    "x = 'S' if platform == 'shopee' else 'L'",
])
def test_the_sweep_sees_each_shape(snippet):
    """Break-it-once, per shape: each spelling the sweep claims to catch."""
    assert PAIR.search(snippet) or BINARY.search(snippet)


def test_the_three_platform_spelling_is_not_flagged():
    assert not PAIR.search("PLATFORMS = ('shopee', 'lazada', 'tiktok')")
