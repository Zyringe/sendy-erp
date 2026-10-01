"""Truth table: what each reader family answers for the same bill units.

Card A (unit conversion module) gives every reader of `unit_conversions` one
home but keeps each family's own rule (ADR 0019). These rows pin those rules
so the move cannot quietly unify them. Every family reads the SAME product:

    unit_type 'ตัว'
    unit_map  หล -> โหล, กร -> กุรุส            (BSN5657)
    unit_conversions  โหล 12 · กร 144 · ตัว 5 (a rogue base-keyed row) · แผง 0

and is asked about the same units:

    None, ''       blank bill unit
    'ตัว'          the base unit (the rogue row says 5)
    ' ตัว'         the base unit with a leading space
    'โหล', 'กร'    a row stored under exactly that spelling
    'หล'           a variant whose WORD has a row, its own spelling none
    'กุรุส'        a word whose row is stored under its CODE (กร)
    'แผง'          a row with ratio 0
    'กล่อง'        no row at all

Where two families disagree, the disagreement is the expected answer. The
SQL twin gives the กุรุส line 1.0 and counts it, the word family gives 144:
test_migration_190_express_unit_meanings pins the same shape on COGS.
"""
import importlib.util
import os
import sys

import pytest

import price_lookup
import review_rules
import sales_filters
import unit_conversion
from models import bsn_sync, ecommerce_overview, mapping, vat_sub
import call_card

UNITS = [None, '', 'ตัว', ' ตัว', 'โหล', 'หล', 'กุรุส', 'กร', 'แผง', 'กล่อง']

_CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'scripts', 'price_lookup_cli.py')


def _product(conn, name, base_sell_price=100.0, unit_type='ตัว'):
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price) VALUES (?, ?, ?)",
        (name, unit_type, base_sell_price)).lastrowid
    conn.executemany(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, ?, ?)",
        [(pid, 'โหล', 12.0), (pid, 'กร', 144.0), (pid, 'ตัว', 5.0), (pid, 'แผง', 0.0)])
    return pid


@pytest.fixture
def conn(empty_db_conn):
    empty_db_conn.executemany(
        "INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', ?, ?)",
        [('หล', 'โหล'), ('กร', 'กุรุส')])
    empty_db_conn.commit()
    return empty_db_conn


# ── word: the price resolver's bill-unit lookup ─────────────────────────────

def _word(conn, pid, unit):
    return unit_conversion.word_ratio(conn, pid, 'ตัว', unit, {})


WORD = {None: 1.0, '': 1.0, 'ตัว': 1.0, ' ตัว': None, 'โหล': 12.0, 'หล': 12.0,
        'กุรุส': 144.0, 'กร': 144.0, 'แผง': 0.0, 'กล่อง': None}


def test_word_family(conn):
    pid = _product(conn, 'word')
    assert {u: _word(conn, pid, u) for u in UNITS} == WORD


def test_word_cache_is_per_product(conn):
    a = _product(conn, 'word cache a')
    b = _product(conn, 'word cache b')
    conn.execute("UPDATE unit_conversions SET ratio = 10 WHERE product_id = ? AND bsn_unit = 'โหล'", (b,))
    cache = {}
    assert [unit_conversion.word_ratio(conn, pid, 'ตัว', 'โหล', cache) for pid in (a, b)] == [12.0, 10.0]


# ── conversion: the row lookup resolve/bundle build on (no short-circuit) ───

def _conversion(conn, pid, unit):
    return unit_conversion.conversion_ratio(conn, pid, unit)


CONVERSION = {None: None, '': None, 'ตัว': 5.0, ' ตัว': None, 'โหล': 12.0, 'หล': None,
              'กุรุส': 144.0, 'กร': 144.0, 'แผง': 0.0, 'กล่อง': None}


def test_conversion_family(conn):
    pid = _product(conn, 'conversion')
    assert {u: _conversion(conn, pid, u) for u in UNITS} == CONVERSION


# ── resolve: the row FIRST, the base unit second ────────────────────────────

RESOLVE = {'ตัว': (5.0, 'unit_conversions'), ' ตัว': (1.0, 'none'),
           'โหล': (12.0, 'unit_conversions'), 'หล': (1.0, 'none'),
           'กุรุส': (144.0, 'unit_conversions'), 'กร': (144.0, 'unit_conversions'),
           'แผง': (0.0, 'unit_conversions'), 'กล่อง': (1.0, 'none')}


def test_resolve_family(conn):
    """Asked units only: `_normalize_unit` never hands it a blank."""
    pid = _product(conn, 'resolve')
    got = {u: price_lookup._resolve_unit(conn, pid, u, 'ตัว')[:2] for u in RESOLVE}
    assert got == RESOLVE
    with pytest.raises(ValueError):
        price_lookup._resolve_unit(conn, pid, 'กล่อง', 'ตัว', strict=True)


def test_resolve_tier_implied_dozen(conn):
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('tier', 'ตัว')").lastrowid
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (?, '1 โหล', 100)", (pid,))
    assert price_lookup._resolve_unit(conn, pid, 'โหล', 'ตัว')[:2] == (12.0, 'tier-implied')


# ── bundle: word lookup, silent 1.0 on a miss (Q16) ─────────────────────────

BUNDLE = {None: 1.0, '': 1.0, 'ตัว': 1.0, ' ตัว': 1.0, 'โหล': 12.0, 'หล': 12.0,
          'กุรุส': 144.0, 'กร': 144.0, 'แผง': 0.0, 'กล่อง': 1.0}


def test_bundle_family(conn):
    pid = _product(conn, 'bundle')
    assert {u: price_lookup._bundle_buy_ratio(conn, pid, u, 'ตัว') for u in UNITS} == BUNDLE


# ── SQL: COGS base qty, exact join, 1.0 + counted on a miss ─────────────────

SQL = {None: (1.0, 0), '': (1.0, 0), 'ตัว': (1.0, 0), ' ตัว': (1.0, 1),
       'โหล': (12.0, 0), 'หล': (1.0, 1), 'กุรุส': (1.0, 1), 'กร': (144.0, 0),
       'แผง': (0.0, 0), 'กล่อง': (1.0, 1)}


def test_sql_family(conn):
    pid = _product(conn, 'sql')
    got = {}
    for i, u in enumerate(UNITS):
        doc = f'IVSQL-{i}'
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, product_id, qty, unit, net) "
            "VALUES ('2026-09-01', ?, ?, 1, ?, 10)", (doc, pid, u))
        got[u] = conn.execute(
            "SELECT " + sales_filters.base_qty_sql() + ", " + sales_filters.unratioed_line_sql()
            + " FROM sales_transactions st LEFT JOIN products p ON p.id = st.product_id "
            + sales_filters.unit_conversion_join() + " WHERE st.doc_no = ?", (doc,)).fetchone()[:]
    assert got == SQL


# ── overview: marketplace units sold since the stock file ───────────────────

# (sold, lines counted at 1 for want of a ratio). Q14 gave this family the
# base-unit short-circuit, so the rogue ตัว=5 row no longer reads 5.
OVERVIEW = {None: (1, 0), '': (1, 0), 'ตัว': (1, 0), ' ตัว': (1, 1), 'โหล': (12, 0),
            'หล': (1, 1), 'กุรุส': (1, 1), 'กร': (144, 0), 'แผง': (0, 0), 'กล่อง': (1, 1)}


def _overview(conn, pid):
    return ecommerce_overview._sold_since_by_pid(conn, 'shopee', '2026-01-01', [pid]).get(pid)


def test_overview_family(conn):
    got = {}
    for i, u in enumerate(UNITS):
        pid = _product(conn, f'overview {i}')
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, product_id, customer, qty, unit, "
            "net, synced_to_stock) VALUES ('2026-06-15', ?, ?, 'หน้าร้านS', 1, ?, 10, 0)",
            (f'IVOV-{i}', pid, u))
        got[u] = _overview(conn, pid)
    assert got == OVERVIEW


# ── options: every stored row, for the VAT-sub unit selector ────────────────

def test_options_family(conn):
    pid = _product(conn, 'options')
    got = sorted((o['unit'], o['ratio'], o['is_base']) for o in vat_sub.get_unit_options(pid, conn))
    assert got == sorted([('ตัว', 1.0, True), ('โหล', 12.0, False),
                          ('กร', 144.0, False), ('แผง', 0.0, False)])


# ── exact: the call card (batch), the quote CLI, the stock writer, review ───

# unit -> (base, customer_price) with a fixed promo of ฿8 per piece. A unit
# with no usable ratio keeps the unconverted base and skips the fixed promo
# (#668); a ratio-0 row is no ratio at all.
CALL_CARD = {None: (100.0, 8.0), '': (100.0, 8.0), 'ตัว': (100.0, 8.0),
             ' ตัว': (100.0, 100.0), 'โหล': (1200.0, 96.0), 'หล': (100.0, 100.0),
             'กุรุส': (100.0, 100.0), 'กร': (14400.0, 1152.0), 'แผง': (100.0, 100.0),
             'กล่อง': (100.0, 100.0)}


def test_call_card_family(conn):
    pid = _product(conn, 'card')
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, is_active) "
        "VALUES (?, 'fixed 8', 'fixed', 8, 1)", (pid,))
    for i, u in enumerate(UNITS):
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
            "qty, unit, net, vat_type) VALUES ('2026-09-01', ?, ?, ?, 'ร้านทดสอบ', 1, ?, ?, 1)",
            (f'IVCC-{i}', f'IVCC{i}', pid, u, 1000 + i))
    # Card C P3: the customer key ('ร้านทดสอบ', a bill name: these rows carry no code) replaces the name list.
    products = call_card._assemble_products(conn, 'ร้านทดสอบ', None, today='2026-09-29')
    got = sorted((p['unit'], p['base'], p['customer_price']) for p in products)
    want = sorted(((u or ''), b, c) for u, (b, c) in CALL_CARD.items())
    assert got == want


def _load_cli():
    spec = importlib.util.spec_from_file_location('price_lookup_cli_families', _CLI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['price_lookup_cli_families'] = mod
    spec.loader.exec_module(mod)
    return mod


# cash per piece from one ฿120 bill. Ratio 0 divides by zero: pre-existing, and
# both ratio writers refuse ratio <= 0, so only raw SQL can store one.
CLI = {None: None, '': None, 'ตัว': 120.0, ' ตัว': None, 'โหล': 10.0, 'หล': None,
       'กุรุส': None, 'กร': 0.83, 'กล่อง': None}


def test_cli_family(conn):
    cli = _load_cli()
    got = {}
    for i, u in enumerate(list(CLI) + ['แผง']):
        pid = _product(conn, f'cli {i}')
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
            "qty, unit, net, vat_type) VALUES ('2026-09-01', ?, ?, ?, 'ร้านทดสอบ', 1, ?, 120, 1)",
            (f'IVCLI-{i}', f'IVCLI{i}', pid, u))
        if u == 'แผง':
            with pytest.raises(ZeroDivisionError):
                cli._latest_cash_per_piece(conn, pid, 'ตัว')
        else:
            got[u] = cli._latest_cash_per_piece(conn, pid, 'ตัว')
    assert got == CLI


# ── exact, stock side: the stock writer, the remap preflight, review rules ──

# Stock compares after strip() and rounds only a converted qty to 4 dp: the
# base branch hands back the qty itself (int stays int), a ratio-0 row gives
# 0.0. The lookup is the raw spelling.
STOCK = {None: None, '': None, 'ตัว': 1, ' ตัว': 1, 'โหล': 12.0, 'หล': None,
         'กุรุส': None, 'กร': 144.0, 'แผง': 0.0, 'กล่อง': None}


def test_stock_family(conn):
    pid = _product(conn, 'stock')
    got = {u: bsn_sync._get_base_qty(conn, pid, 'ตัว', u, 1) for u in UNITS}
    assert {u: repr(v) for u, v in got.items()} == {u: repr(v) for u, v in STOCK.items()}


def test_stock_rounds_only_the_converted_branch(conn):
    pid = _product(conn, 'stock round')
    assert repr(bsn_sync._get_base_qty(conn, pid, 'ตัว', 'ตัว', 0.12345)) == '0.12345'
    assert repr(bsn_sync._get_base_qty(conn, pid, 'ตัว', 'โหล', 0.12345)) == '1.4814'


# unit_type stored with a trailing space: stock strips both sides, review
# compares raw and so reads the rogue ตัว=5 row instead.
UNITS_WS = ['ตัว', 'ตัว ', ' ตัว', '\u2003ตัว']
STOCK_WS = {'ตัว': 1, 'ตัว ': 1, ' ตัว': 1, '\u2003ตัว': 1}
REVIEW_WS = {'ตัว': (5.0, True), 'ตัว ': (1.0, True), ' ตัว': (None, False),
             '\u2003ตัว': (None, False)}
MISSING_WS = {'ตัว': [], 'ตัว ': [], ' ตัว': [], '\u2003ตัว': []}


def test_stock_side_with_whitespace_in_unit_type(conn):
    pid = _product(conn, 'ws', unit_type='ตัว ')
    assert {u: bsn_sync._get_base_qty(conn, pid, 'ตัว ', u, 1) for u in UNITS_WS} == STOCK_WS
    assert {u: review_rules._get_ratio(conn, pid, u, 'ตัว ') for u in UNITS_WS} == REVIEW_WS
    assert {u: mapping.missing_unit_ratios(conn, pid, [{'unit': u}])
            for u in UNITS_WS} == MISSING_WS


def test_stock_with_a_null_unit_type_raises(conn):
    """products.unit_type is NOT NULL; the writer never guarded it."""
    pid = _product(conn, 'null type')
    with pytest.raises(AttributeError):
        bsn_sync._get_base_qty(conn, pid, None, 'ตัว', 1)


REVIEW = {None: (None, False), '': (None, False), 'ตัว': (1.0, True), ' ตัว': (None, False),
          'โหล': (12.0, True), 'หล': (None, False), 'กุรุส': (None, False),
          'กร': (144.0, True), 'แผง': (0.0, True), 'กล่อง': (None, False)}


def test_review_rules_family(conn):
    pid = _product(conn, 'review')
    assert {u: review_rules._get_ratio(conn, pid, u, 'ตัว') for u in UNITS} == REVIEW


# The remap preflight: which of the rows' units the destination could not
# convert. A ratio-0 row exists, so it is not missing.
MISSING = {None: [None], '': [''], 'ตัว': [], ' ตัว': [], 'โหล': [], 'หล': ['หล'],
           'กุรุส': ['กุรุส'], 'กร': [], 'แผง': [], 'กล่อง': ['กล่อง']}


def test_remap_preflight_family(conn):
    pid = _product(conn, 'remap')
    assert {u: mapping.missing_unit_ratios(conn, pid, [{'unit': u}]) for u in UNITS} == MISSING
    assert mapping.missing_unit_ratios(
        conn, pid, [{'unit': u} for u in ('หล', 'กล่อง', 'โหล', 'หล')]) == ['กล่อง', 'หล']
