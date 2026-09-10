"""Card 3 of the 2026-09-08 architecture review — "a product's price has no owner".

Three modules answered "what does this promo do to the price?" differently for a
`mixed` row: models.promotions.effective_price ignored it, price_lookup.apply_price_promo
applied discount_value as a percent, review_rules._promo_expected_per_base_unit skipped it.
Measured on PROD 2026-09-08: 28 active `mixed` rows, 27 carrying a percent, all own-brand,
so /products/<id> showed a price 10-20% above the one the quote resolver gave the customer.

The schema settles which answer is right, so this is a defect, not a convention:
  - the promotions CHECK forbids discount_value on `bundle` and `gift`, and allows any
    combination on `mixed` ("At least one structured field populated");
  - promo_slot_sql()'s price_expr already puts `mixed AND discount_value IS NOT NULL`
    in the PRICE slot, and migration 177 renders that same expression into a DB trigger;
  - the promo form labels the type "ผสม (% + แถม / + ของแถม)" and macros.html renders
    "ลด N%" for it.

Every DB-backed test forces its own state on a throwaway product (`tmp_db_conn` clones the
live dev DB WITH data — never inherit it) and asserts a COUNT before any property.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

# ⚠ `import price_lookup` WAS order-dependent in this repo until #476 renamed the CLI
# wrapper to scripts/price_lookup_cli.py: two modules shared the name (the resolver in
# inventory_app/ and the wrapper in scripts/), and three module-level
# `sys.path.insert(0, .../scripts)` calls put the wrapper first (name_builder.py:17,
# bsn_suggest.py:24, blueprints/bsn.py:500). The inserts are still there;
# tests/test_price_lookup_import_shadow.py now guards the name.
#
# Load the resolver by PATH so the result cannot depend on collection order, and keep the
# assert as a control: if the loader ever returns the wrong file, this fails loudly.
import importlib.util as _ilu
import os as _os

_RESOLVER = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    'inventory_app', 'price_lookup.py')
_spec = _ilu.spec_from_file_location('inv_price_lookup_under_test', _RESOLVER)
pl = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(pl)
assert pl.__file__.endswith(_os.path.join('inventory_app', 'price_lookup.py')), (
    f'wrong price_lookup loaded: {pl.__file__}')
assert hasattr(pl, 'apply_price_promo'), 'control: the resolver must expose apply_price_promo'

from models import promotions as promo_models
import review_rules


# ── helpers ──────────────────────────────────────────────────────────────────

def _promo(promo_type, discount_value=None, bundle_buy=None, bundle_free=None,
           gift_desc=None, gift_qty=None):
    """A promo row as a plain dict — every consumer here reads it by key only."""
    return {
        'promo_type': promo_type, 'discount_value': discount_value,
        'bundle_buy': bundle_buy, 'bundle_free': bundle_free,
        'gift_desc': gift_desc, 'gift_qty': gift_qty,
    }


@pytest.fixture
def product(tmp_db_conn):
    """A throwaway product with base_sell_price 90.00 and NO promotions."""
    cur = tmp_db_conn.execute(
        "INSERT INTO products (product_name, base_sell_price, is_active) VALUES (?,?,1)",
        ('TEST card3 promo owner', 90.00))
    pid = cur.lastrowid
    tmp_db_conn.execute("DELETE FROM promotions WHERE product_id = ?", (pid,))
    tmp_db_conn.commit()
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE product_id = ?", (pid,)).fetchone()[0] == 0
    return {'id': pid, 'base_sell_price': 90.00}


def _add_promo(conn, pid, promo_type, **kw):
    cols = ['product_id', 'promo_name', 'promo_type', 'is_active']
    vals = [pid, f'test {promo_type}', promo_type, 1]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    conn.execute(f"INSERT INTO promotions ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(vals))})", vals)
    conn.commit()


# ── A · one implementation of "promo → price" ────────────────────────────────

CASES = [
    (_promo('percent', 20.0),                       100.0, 1.0, 80.0),
    (_promo('fixed', 45.0),                         100.0, 1.0, 45.0),
    (_promo('fixed', 20.0),                         240.0, 12.0, 240.0),
    (_promo('fixed', 20.0),                         500.0, None, 500.0),
    (_promo('mixed', 20.0, bundle_buy=12, bundle_free=1), 90.0, 1.0, 72.0),
    (_promo('mixed', None, bundle_buy=12, bundle_free=1), 90.0, 1.0, 90.0),
    (_promo('bundle', None, bundle_buy=1, bundle_free=1), 90.0, 1.0, 90.0),
    (_promo('gift', None, gift_desc='ดจ.สแตนเลส', gift_qty='20'), 90.0, 1.0, 90.0),
    (None,                                          90.0, 1.0, 90.0),
]


@pytest.mark.parametrize('promo,list_for_unit,ratio,expected', CASES)
def test_promo_price_is_the_one_implementation(promo, list_for_unit, ratio, expected):
    assert promo_models.promo_price(list_for_unit, ratio, promo) == expected


def test_apply_price_promo_delegates_and_cannot_drift():
    """price_lookup's public entry point must return exactly what the owner returns,
    for every case — this is the guard that stops the two copies diverging again."""
    assert len(CASES) == 9, 'the agreement matrix must cover every promo_type'
    for promo, list_for_unit, ratio, expected in CASES:
        assert pl.apply_price_promo(list_for_unit, ratio, promo) == \
            promo_models.promo_price(list_for_unit, ratio, promo)


# ── B · the Python predicate and the SQL predicate must agree ────────────────

def test_affects_price_matches_promo_slot_sql_on_every_row(tmp_db_conn):
    """affects_price() is the Python twin of promo_slot_sql()'s price_expr. Two
    spellings of one rule is exactly the defect this card is about, so pin them
    against each other over the whole table."""
    price_expr, _qty = promo_models.promo_slot_sql('')
    rows = tmp_db_conn.execute(
        f"SELECT *, ({price_expr}) AS sql_says FROM promotions").fetchall()
    assert len(rows) > 0, 'control: the promotions table must not be empty'
    assert any(r['sql_says'] for r in rows), 'control: some row must affect price'
    assert any(not r['sql_says'] for r in rows), 'control: some row must not'
    for r in rows:
        assert promo_models.affects_price(r) is bool(r['sql_says']), \
            f"row {r['id']} ({r['promo_type']}, d={r['discount_value']}) disagrees"


def test_affects_price_on_none():
    assert promo_models.affects_price(None) is False


# ── C · effective_price ──────────────────────────────────────────────────────

def test_effective_price_applies_a_mixed_rows_percent(tmp_db_conn, product):
    """THE live defect: 27 products on PROD 2026-09-08 render base while the quote
    resolver charges base × (1 − d/100)."""
    _add_promo(tmp_db_conn, product['id'], 'mixed',
               discount_value=20.0, bundle_buy=12, bundle_free=1)
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE product_id = ?",
        (product['id'],)).fetchone()[0] == 1
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 72.00


def test_effective_price_leaves_a_mixed_row_with_no_percent_alone(tmp_db_conn, product):
    _add_promo(tmp_db_conn, product['id'], 'mixed', bundle_buy=12, bundle_free=1)
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 90.00


def test_effective_price_reads_the_price_slot_not_the_newest_row(tmp_db_conn, product):
    """get_active_promotion() ordered by created_at across ALL types, so a later
    bundle promo shadowed an earlier percent one and the discount vanished.
    Measured 0 products on PROD 2026-09-08 — latent, not live, but reachable:
    migration 177 permits one promo per slot, i.e. both at once."""
    _add_promo(tmp_db_conn, product['id'], 'percent', discount_value=10.0,
               created_at='2026-01-01 00:00:00')
    _add_promo(tmp_db_conn, product['id'], 'bundle', bundle_buy=1, bundle_free=1,
               created_at='2026-06-01 00:00:00')
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE product_id = ?",
        (product['id'],)).fetchone()[0] == 2
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 81.00


def test_effective_price_percent_unchanged(tmp_db_conn, product):
    _add_promo(tmp_db_conn, product['id'], 'percent', discount_value=10.0)
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 81.00


def test_effective_price_fixed_unchanged(tmp_db_conn, product):
    _add_promo(tmp_db_conn, product['id'], 'fixed', discount_value=45.0)
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 45.00


def test_effective_price_no_promo_is_base(tmp_db_conn, product):
    assert promo_models.effective_price(product, conn=tmp_db_conn) == 90.00


# ── D · review_rules R5 ──────────────────────────────────────────────────────

def test_r5_expects_a_price_for_a_mixed_row_with_a_percent():
    """R5_PROMO_MISMATCH skipped every mixed row, so 27 own-brand products were
    exempt from the bill-review price check."""
    got = review_rules._promo_expected_per_base_unit(
        {'base_sell_price': 90.00},
        _promo('mixed', 20.0, bundle_buy=12, bundle_free=1))
    assert got == 72.00


def test_r5_skips_a_mixed_row_with_no_percent():
    assert review_rules._promo_expected_per_base_unit(
        {'base_sell_price': 90.00},
        _promo('mixed', None, bundle_buy=12, bundle_free=1)) is None


def test_r5_skips_bundle_and_gift():
    for p in (_promo('bundle', None, bundle_buy=1, bundle_free=1),
              _promo('gift', None, gift_desc='x', gift_qty='1')):
        assert review_rules._promo_expected_per_base_unit(
            {'base_sell_price': 90.00}, p) is None


def test_r5_percent_and_fixed_unchanged():
    assert review_rules._promo_expected_per_base_unit(
        {'base_sell_price': 90.00}, _promo('percent', 10.0)) == 81.00
    assert review_rules._promo_expected_per_base_unit(
        {'base_sell_price': 90.00}, _promo('fixed', 45.0)) == 45.00
