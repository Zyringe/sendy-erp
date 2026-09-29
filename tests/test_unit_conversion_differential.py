"""Old vs new, per reader family, over synthetic edge inputs (card A, PR1).

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

BASELINE = 'ccb2245'
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
    return mod


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


def test_resolve_and_bundle(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    for pid, ut in products.items():
        for u in SPELLINGS + ['ลัง']:
            for strict in (False, True):
                assert (_call(old._resolve_unit, conn, pid, u, ut, strict)
                        == _call(price_lookup._resolve_unit, conn, pid, u, ut, strict)), (pid, u)
        for u in BILL_UNITS:
            assert (_call(old._bundle_buy_ratio, conn, pid, u, ut)
                    == _call(price_lookup._bundle_buy_ratio, conn, pid, u, ut)), (pid, u)


def test_resolve_price_end_to_end(world):
    conn, products = world
    old = _old('inventory_app/price_lookup.py', '_old_price_lookup')
    answered = 0
    for pid in products:
        for u in [None, 'โหล', 'หล', 'กุรุส', 'แผง']:
            for cust in (None, 'ร้านทดสอบ'):
                args = dict(product_id=pid, customer_code=cust, unit=u, qty=2, today='2026-09-29')
                got = _call(lambda: price_lookup.resolve_price(conn, **args))
                assert _call(lambda: old.resolve_price(conn, **args)) == got, (pid, u)
                answered += not got.startswith('raises')
    assert answered > 100, 'control: most asks must resolve, not raise'


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


def test_call_card(world):
    conn, _ = world
    old = _old('inventory_app/call_card.py', '_old_call_card')
    for names in (['ร้านทดสอบ'], ['หน้าร้านS']):
        assert (old._assemble_products(conn, names, None, today='2026-09-29')
                == call_card._assemble_products(conn, names, None, today='2026-09-29'))


STOCK_UNIT_TYPES = UNIT_TYPES + ['ตัว ', ' ตัว', ' ตัว', '']
QTYS = [1, 0, 6, 2.5, 0.12345, 1000]


def test_stock_side_exact_family(world):
    """PR2: the stock writer, review rules and the remap preflight."""
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
