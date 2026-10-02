"""Old vs new, per reader family, over synthetic edge inputs (card A).

Each old function is loaded from the baseline commit with `git show` and run
beside its replacement on the SAME database: random products, spellings and
ratios from a fixed seed, dense in the cases prod does not hold today
(variant spellings, whitespace, ratio 0, rows keyed on the base unit,
code-keyed rows). Answers are compared by repr, so 12 vs 12.0 is a
difference too.

The one difference allowed is Q14: the marketplace sold count now
short-circuits a line in the product's own unit, so a row keyed on the base
unit with a ratio other than 1 no longer scales it (PROD 2026-09-29: 0 such
rows). The test asserts the difference lands on exactly those products.

Since #716 resolve_price also answers where the baseline raised
ZeroDivisionError: a bill whose unit has a ratio-0 row is now skipped as
unratioed instead of divided by. Since #720 `_resolve_unit` skips a ratio-0
row (the baseline returned 0.0), so asks on a product carrying one may
differ, or raise ValueError where the baseline priced at ratio 0.
"""
import functools
import importlib.util
import os
import random
import subprocess
import sys

import pytest

import price_lookup
import unit_conversion
import call_card
from models import ecommerce_overview, vat_sub

BASELINE = 'ccb22456e3bc854fbef99f7a30ae092f3bf01254'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = 20260929

UNIT_TYPES = ['ตัว', 'โหล', 'แผง', 'กุรุส']
SPELLINGS = ['ตัว', 'โหล', 'หล', 'กร', 'กุรุส', 'กล่อง', 'แผง', 'บล', ' ตัว', 'ตัว ']
BILL_UNITS = SPELLINGS + ['', None, 'ลัง']
RATIOS = [0.0, 0.5, 1.0, 5.0, 12.0, 144.0]
UNIT_MAP = [('BSN5657', 'หล', 'โหล'), ('BSN5657', 'กร', 'กุรุส'), ('BSN5657', 'บล', 'แผง'),
            ('*', 'ตัว ', 'ตัว')]


@functools.lru_cache(maxsize=None)
def _old(path, name, package=None):
    try:
        src = subprocess.run(['git', 'show', f'{BASELINE}:{path}'], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f'baseline {BASELINE} not readable with git')
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = os.path.join(ROOT, path)
    if package:
        mod.__package__ = package
    sys.modules[name] = mod
    exec(compile(src, f'{BASELINE}:{path}', 'exec'), mod.__dict__)
    if 'promo_models' in mod.__dict__:
        mod.promo_models = _baseline_promo_models()
    return mod


def _baseline_promo_models():
    """models.promotions as the baseline called it. #673 made promo_price's
    quantity keywords required; the baseline passes three arguments. The world
    below holds no promo with a minimum, so the baseline's call IS the
    no-quantity call (qty_pieces=None): the gate answers 'none' every time."""
    import types
    from models import promotions as current
    shim = types.SimpleNamespace(**vars(current))
    shim.promo_price = lambda list_for_unit, ratio, promo: current.promo_price(
        list_for_unit, ratio, promo, qty_pieces=None, min_pieces=None)
    return shim


def _call(fn, *args):
    try:
        return repr(fn(*args))
    except Exception as e:  # the exception type is part of the behaviour
        return f'raises {type(e).__name__}'


@pytest.fixture
def world(empty_db_conn):
    """{pid: unit_type} for 40 products with random rows, plus the unit map."""
    conn = empty_db_conn
    rng = random.Random(SEED)
    conn.executemany("INSERT INTO unit_map (book, spelling, word) VALUES (?, ?, ?)", UNIT_MAP)
    products = {}
    for i in range(40):
        ut = rng.choice(UNIT_TYPES)
        pid = conn.execute(
            "INSERT INTO products (product_name, unit_type, base_sell_price) VALUES (?, ?, ?)",
            (f'diff {i}', ut, rng.choice([0, 10, 100]))).lastrowid
        keys = rng.sample(SPELLINGS, rng.randint(0, 5))
        if i % 5 == 0 and ut not in keys:
            keys.append(ut)                       # a row keyed on the base unit
        for k in keys:
            conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                         "VALUES (?, ?, ?)", (pid, k, rng.choice(RATIOS)))
        if rng.random() < 0.4:
            conn.execute("INSERT INTO promotions (product_id, promo_name, promo_type, "
                         "discount_value, is_active) VALUES (?, 'p', ?, ?, 1)",
                         (pid, rng.choice(['fixed', 'percent']), rng.choice([5, 8, 20])))
        products[pid] = ut
    for j in range(300):
        pid = rng.choice(list(products))
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
            "qty, unit, net, vat_type, synced_to_stock) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (f'2026-0{rng.randint(6, 9)}-1{rng.randint(0, 9)}', f'IVD{j}-1', f'IVD{j}', pid,
             rng.choice(['ร้านทดสอบ', 'หน้าร้านS', 'หน้าร้านL']),
             rng.choice([1, 2, 3.5]), rng.choice(BILL_UNITS), rng.choice([60, 120, 1000]),
             rng.choice([0, 1, 2])))
    conn.commit()
    return conn, products


def test_word_ratio(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    for pid, ut in products.items():
        old_cache, new_cache = {}, {}
        for u in BILL_UNITS + BILL_UNITS:          # twice: the cache answers the repeat
            assert (_call(old._bill_ratio, conn, pid, ut, u, old_cache)
                    == _call(unit_conversion.word_ratio, conn, pid, ut, u, new_cache)), (pid, ut, u)


def test_conversion_ratio(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    for pid in products:
        for u in BILL_UNITS:
            assert (_call(old._conversion_ratio, conn, pid, u)
                    == _call(unit_conversion.conversion_ratio, conn, pid, u)), (pid, u)


def _asks_a_ratio_zero_word(conn, pid, unit):
    """Does the asked unit's WORD have a ratio <= 0 row on this product?"""
    if not unit:
        return False
    word = price_lookup._unit_word(conn, unit)
    return any(price_lookup._unit_word(conn, r[0]) == word for r in conn.execute(
        "SELECT bsn_unit FROM unit_conversions WHERE product_id = ? AND ratio <= 0", (pid,)))


def test_resolve_and_bundle(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    fixed_720 = 0
    for pid, ut in products.items():
        for u in SPELLINGS + ['ลัง']:
            for strict in (False, True):
                want = _call(old._resolve_unit, conn, pid, u, ut, strict)
                got = _call(price_lookup._resolve_unit, conn, pid, u, ut, strict)
                if want.startswith('(0.0, '):   # #720: the baseline handed back a ratio-0 row
                    assert not got.startswith('(0.0, '), (pid, u)
                    fixed_720 += 1
                    continue
                assert want == got, (pid, u)
    assert fixed_720 > 0, 'control: the seed still holds a ratio-0 row the baseline returned'
    for pid, ut in products.items():
        for u in BILL_UNITS:
            assert (_call(old._bundle_buy_ratio, conn, pid, u, ut)
                    == _call(price_lookup._bundle_buy_ratio, conn, pid, u, ut)), (pid, u)


def test_resolve_price_end_to_end(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    answered = fixed_716 = fixed_720 = 0
    for pid in products:
        for u in [None, 'โหล', 'หล', 'กุรุส', 'แผง']:
            for cust in (None, 'ร้านทดสอบ'):
                args = dict(product_id=pid, customer_code=cust, unit=u, qty=2, today='2026-09-29')
                got = _call(lambda: price_lookup.resolve_price(conn, **args))
                want = _call(lambda: old.resolve_price(conn, **args))
                if want == 'raises ZeroDivisionError' and not got.startswith('raises'):
                    fixed_716 += 1
                    continue
                if want != got and _asks_a_ratio_zero_word(conn, pid, u):   # #720
                    assert got == 'raises ValueError' or not got.startswith('raises'), (pid, u, got)
                    fixed_720 += 1
                    continue
                assert want == got, (pid, u)
                answered += not got.startswith('raises')
    assert answered > 100, 'control: most asks must resolve, not raise'
    assert fixed_716 > 0, 'control: the seed still holds a ratio-0 bill the baseline divided by'
    assert fixed_720 > 0, 'control: an ask on a ratio-0 product moved'


def _sql_rows(conn, sf):
    return conn.execute(
        "SELECT st.id, " + sf.base_qty_sql() + ", " + sf.unratioed_line_sql()
        + " FROM sales_transactions st LEFT JOIN products p ON p.id = st.product_id "
        + sf.unit_conversion_join() + " ORDER BY st.id").fetchall()


def test_sql_trio(world):
    conn, _ = world
    old = _old('inventory_app/sales_filters.py', '_old_sales_filters')
    assert [tuple(r) for r in _sql_rows(conn, old)] == [tuple(r) for r in _sql_rows(conn, unit_conversion)]


def test_marketplace_sold_count_moves_only_where_q14_says(world):
    conn, products = world
    old = _old('inventory_app/models/ecommerce_overview.py', 'models._old_ecommerce_overview',
               package='models')
    rogue = {r[0] for r in conn.execute(
        "SELECT DISTINCT st.product_id FROM sales_transactions st "
        "JOIN products p ON p.id = st.product_id "
        "JOIN unit_conversions uc ON uc.product_id = p.id AND uc.bsn_unit = p.unit_type "
        "WHERE st.unit = p.unit_type AND uc.ratio <> 1")}
    assert rogue, 'control: the seed must produce a base-keyed row a line would hit'
    pids = sorted(products)
    moved = set()
    for platform in ('shopee', 'lazada'):
        before = old._sold_since_by_pid(conn, platform, '2026-01-01', pids)
        after = ecommerce_overview._sold_since_by_pid(conn, platform, '2026-01-01', pids)
        assert set(before) == set(after)
        moved |= {pid for pid in before if before[pid] != pytest.approx(after[pid][0])}
    assert moved and moved <= rogue


def test_unit_options(world):
    conn, products = world
    old = _old('inventory_app/models/vat_sub.py', 'models._old_vat_sub', package='models')
    for pid in products:
        assert old.get_unit_options(pid, conn) == vat_sub.get_unit_options(pid, conn), pid


def test_quote_cli_latest_cash(world):
    conn, products = world
    old = _old('scripts/price_lookup_cli.py', '_old_price_lookup_cli')
    new = _new_cli()
    for pid, ut in products.items():
        assert (_call(old._latest_cash_per_piece, conn, pid, ut)
                == _call(new._latest_cash_per_piece, conn, pid, ut)), pid


def _new_cli():
    spec = importlib.util.spec_from_file_location(
        '_new_price_lookup_cli', os.path.join(ROOT, 'scripts', 'price_lookup_cli.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules['_new_price_lookup_cli'] = mod
    spec.loader.exec_module(mod)
    return mod


# Card C P3 moved four fields of the call card's rows onto purchase_history: which rows
# are listed, and per row `total_qty`, `total_net` (documents invoiced in error out),
# `doc_count` (purchases, not documents) and `last_buy` (last PAID purchase). Everything
# else (base, customer_price, promo, tiers, peers, latest price, orders, flag ...) is
# still card A's arithmetic and must equal the baseline on every row the two lists share.
CARD_C_FIELDS = {'total_qty', 'total_net', 'doc_count', 'last_buy'}
# #668 moved the card's ratio onto the word family (stripped unit) and made a miss
# carry no price; these fields are checked against the ratio instead.
CARD_668_FIELDS = {'base', 'customer_price', 'ratio_missing'}


def _exact_row(conn, pid, unit):
    """The baseline card's lookup: that spelling only, ratio 0 is none."""
    row = conn.execute("SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
                       (pid, unit)).fetchone()
    return float(row[0]) if row and row[0] else None


def test_call_card(world):
    conn, products = world
    old = _old('inventory_app/call_card.py', '_old_call_card')
    # The baseline card imports the CURRENT price_lookup and calls
    # apply_price_promo with three arguments; see _baseline_promo_models.
    with pytest.MonkeyPatch.context() as mp:
        current_apply = price_lookup.apply_price_promo
        mp.setattr(price_lookup, 'apply_price_promo',
                   lambda list_for_unit, ratio, promo: current_apply(
                       list_for_unit, ratio, promo, qty_pieces=None, min_pieces=None))
        got_old = {(p['product_id'], p['unit']): p for p in
                   old._assemble_products(conn, ['ร้านทดสอบ'], None, today='2026-09-29')}
    got_new = {(p['product_id'], p['unit']): p for p in
               call_card._assemble_products(conn, 'ร้านทดสอบ', None, today='2026-09-29')}
    shared = sorted(set(got_old) & set(got_new), key=repr)
    assert len(shared) >= 10, 'control: the two lists must share rows to compare'
    seen = {'same ratio': 0, 'new ratio': 0, 'miss': 0}
    for k in shared:
        old_p, new_p = got_old[k], got_new[k]
        assert new_p.keys() == old_p.keys() | {'ratio_missing'}, k
        for f in sorted(old_p.keys() - CARD_C_FIELDS - CARD_668_FIELDS):
            assert repr(old_p[f]) == repr(new_p[f]), (k, f)
        # #668: the price fields may move only where the ratio itself moved.
        pid, unit = k
        ut = products[pid]
        old_r = 1.0 if not unit or unit == ut else _exact_row(conn, pid, unit)
        new_r = unit_conversion.word_ratio(conn, pid, ut, unit.strip(), {}) or None
        bsp = conn.execute("SELECT base_sell_price FROM products WHERE id = ?", (pid,)).fetchone()[0]
        if new_r is None:
            seen['miss'] += 1
            assert (new_p['base'], new_p['customer_price'], new_p['ratio_missing']) == (None, None, True), k
        elif new_r == old_r:
            seen['same ratio'] += 1
            assert new_p['ratio_missing'] is False, k
            for f in ('base', 'customer_price'):
                assert repr(old_p[f]) == repr(new_p[f]), (k, f)
        else:
            seen['new ratio'] += 1
            assert new_p['ratio_missing'] is False, k
            assert new_p['base'] == round(bsp * new_r, 2), k
    assert all(seen.values()), f'control: every #668 class must occur, got {seen}'
    # A marketplace shop account is never "bought" history (purchase population).
    assert call_card._assemble_products(conn, 'หน้าร้านS', None, today='2026-09-29') == []


STOCK_UNIT_TYPES = UNIT_TYPES + ['ตัว ', ' ตัว', ' ตัว', '']
QTYS = [1, 0, 6, 2.5, 0.12345, 1000]


def test_stock_side_exact_family(world):
    """The stock writer, review rules and the remap preflight."""
    conn, products = world
    old_bsn = _old('inventory_app/models/bsn_sync.py', 'models._old_bsn_sync', package='models')
    old_rr = _old('inventory_app/review_rules.py', '_old_review_rules')
    old_map = _old('inventory_app/models/mapping.py', 'models._old_mapping', package='models')
    import review_rules
    from models import bsn_sync, mapping
    for ut in STOCK_UNIT_TYPES[4:]:
        pid = conn.execute("INSERT INTO products (product_name, unit_type) VALUES ('ws', ?)",
                           (ut,)).lastrowid
        conn.executemany("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                         "VALUES (?, ?, ?)", [(pid, 'ตัว', 5.0), (pid, 'โหล', 12.0)])
        products[pid] = ut
    for pid in products:
        for ut in STOCK_UNIT_TYPES + [None]:
            for u in BILL_UNITS + [' ตัว']:
                for q in QTYS:
                    assert (_call(old_bsn._get_base_qty, conn, pid, ut, u, q)
                            == _call(bsn_sync._get_base_qty, conn, pid, ut, u, q)), (pid, ut, u, q)
                assert (_call(old_rr._get_ratio, conn, pid, u, ut)
                        == _call(review_rules._get_ratio, conn, pid, u, ut)), (pid, ut, u)
        for u in BILL_UNITS + [' ตัว']:
            rows = [{'unit': u}]
            assert (_call(old_map.missing_unit_ratios, conn, pid, rows)
                    == _call(mapping.missing_unit_ratios, conn, pid, rows)), (pid, u)
        rows = [{'unit': u} for u in SPELLINGS]
        assert (_call(old_map.missing_unit_ratios, conn, pid, rows)
                == _call(mapping.missing_unit_ratios, conn, pid, rows)), pid


def test_stock_writer_on_every_ledger_line(world):
    conn, _ = world
    old_bsn = _old('inventory_app/models/bsn_sync.py', 'models._old_bsn_sync', package='models')
    from models import bsn_sync
    lines = conn.execute(
        "SELECT st.product_id, p.unit_type, st.unit, st.qty FROM sales_transactions st "
        "JOIN products p ON p.id = st.product_id").fetchall()
    assert len(lines) == 300
    held = 0
    for r in lines:
        got = _call(bsn_sync._get_base_qty, conn, r[0], r[1], r[2], r[3])
        assert _call(old_bsn._get_base_qty, conn, r[0], r[1], r[2], r[3]) == got, tuple(r)
        held += got == 'None'
    assert 0 < held < len(lines), 'control: the seed must both hold and convert lines'
