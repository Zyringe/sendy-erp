"""vat-substitute — DB-backed read functions: own-stock card (§4.1),
candidate pool (§5 pool), guess list (§5 cold-start), unit options, planning
totals (§4.9). main_conn = real-schema clone (empty_db_conn); book_conn = a
lightweight standalone connection carrying only the 4 tables these functions
touch (products/product_code_mapping/stock_levels/stmas_meta) — the same
minimal-fixture style as test_vat_book_builder.py."""
import sqlite3

import pytest

import models.vat_sub as vs


@pytest.fixture
def book_conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'book.db')
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL,
            bsn_name TEXT NOT NULL,
            product_id INTEGER,
            bsn_unit TEXT NOT NULL DEFAULT '');
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY,
            quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE stmas_meta (
            stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, vatcod TEXT NOT NULL);
    """)
    yield c
    c.close()


def _seed_book(conn, code, name, unit='ตัว', stock=0.0, cost=0.0, stkgrp='', vatcod='1'):
    cur = conn.execute("INSERT INTO products (product_name, unit_type, cost_price) "
                       "VALUES (?, ?, ?)", (name, unit, cost))
    pid = cur.lastrowid
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
                "VALUES (?, ?, ?)", (code, name, pid))
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, ?)", (pid, stock))
    conn.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES (?, ?, ?)",
                (code, stkgrp, vatcod))
    conn.commit()
    return pid


def _seed_main_product(conn, name='สินค้าทดสอบ', unit_type='ตัว', size=None, color_code=None,
                       base_sell_price=0.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, size, color_code, base_sell_price) "
        "VALUES (?, ?, ?, ?, ?)", (name, unit_type, size, color_code, base_sell_price))
    conn.commit()
    return cur.lastrowid


# ── get_own_stock_card (§4.1) ───────────────────────────────────────────────

def test_own_stock_card_none_when_no_identity_mapping(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    assert vs.get_own_stock_card(pid, empty_db_conn, book_conn) is None


def test_own_stock_card_eligible_when_stock_and_vatcod_ok(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    _seed_book(book_conn, 'X1', 'ของเดียวกัน', stock=5, cost=10, vatcod='1')
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('X1', ?, 'auto', 'code+name')", (pid,))
    empty_db_conn.commit()
    card = vs.get_own_stock_card(pid, empty_db_conn, book_conn)
    assert card['xp5_code'] == 'X1'
    assert card['eligible'] is True
    assert card['stock'] == 5


def test_own_stock_card_greyed_when_stock_below_threshold(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    _seed_book(book_conn, 'X1', 'ของเดียวกัน', stock=0.2, cost=10, vatcod='1')
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('X1', ?, 'reviewed', 'manual')", (pid,))
    empty_db_conn.commit()
    card = vs.get_own_stock_card(pid, empty_db_conn, book_conn)
    assert card['eligible'] is False
    assert 'สต็อก' in card['reason']


def test_own_stock_card_greyed_when_vatcod_not_1(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    _seed_book(book_conn, 'X1', 'ของเดียวกัน', stock=5, cost=10, vatcod='0')
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('X1', ?, 'reviewed', 'manual')", (pid,))
    empty_db_conn.commit()
    card = vs.get_own_stock_card(pid, empty_db_conn, book_conn)
    assert card['eligible'] is False
    assert 'VAT' in card['reason']


def test_own_stock_card_ignored_mapping_excluded(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('X1', ?, 'ignored', 'manual')", (pid,))
    empty_db_conn.commit()
    assert vs.get_own_stock_card(pid, empty_db_conn, book_conn) is None


def test_own_stock_card_none_book_conn_shows_unavailable(empty_db_conn):
    pid = _seed_main_product(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('X1', ?, 'reviewed', 'manual')", (pid,))
    empty_db_conn.commit()
    card = vs.get_own_stock_card(pid, empty_db_conn, None)
    assert card['eligible'] is False
    assert card['xp5_code'] == 'X1'


# ── get_candidates (§5 pool) ────────────────────────────────────────────────

def test_candidates_pool_is_union_of_x_groups_deduped_and_filtered(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn, unit_type='ตัว')
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g1, pid))
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g2, pid))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'C1', 'manual')", (g1,))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'C1', 'manual')", (g2,))  # same code, both groups -> dedup
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'C2', 'manual')", (g2,))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'C3', 'manual')", (g2,))  # will fail stock filter
    empty_db_conn.commit()
    _seed_book(book_conn, 'C1', 'ตัวแทน 1', unit='ตัว', stock=3, cost=5, vatcod='1')
    _seed_book(book_conn, 'C2', 'ตัวแทน 2', unit='ตัว', stock=1, cost=8, vatcod='1')
    _seed_book(book_conn, 'C3', 'ตัวแทน 3 (สต็อกต่ำ)', unit='ตัว', stock=0.1, cost=8, vatcod='1')

    cands = vs.get_candidates(pid, empty_db_conn, book_conn)
    codes = [c['xp5_code'] for c in cands]
    assert codes.count('C1') == 1                # deduped across g1+g2
    assert 'C3' not in codes                      # below 0.5 stock threshold
    assert set(codes) == {'C1', 'C2'}


def test_candidates_excludes_own_identity_code(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g1, pid))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'OWN', 'manual')", (g1,))
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('OWN', ?, 'reviewed', 'manual')", (pid,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'OWN', 'ของตัวเอง', stock=5, cost=5, vatcod='1')
    cands = vs.get_candidates(pid, empty_db_conn, book_conn)
    assert cands == []


def test_candidates_excludes_vatcod_not_1(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g1, pid))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'C1', 'manual')", (g1,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'C1', 'VAT ไม่ตรง', stock=5, cost=5, vatcod='0')
    assert vs.get_candidates(pid, empty_db_conn, book_conn) == []


def test_candidates_empty_when_x_has_no_groups(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    assert vs.get_candidates(pid, empty_db_conn, book_conn) == []


def test_candidates_ordered_by_size_color_tiers(empty_db_conn, book_conn):
    empty_db_conn.execute(
        "INSERT INTO color_finish_codes (code, name_th) VALUES ('AC', 'ชุบทอง') "
        "ON CONFLICT(code) DO NOTHING")
    pid = _seed_main_product(empty_db_conn, size='4in', color_code='AC')
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g1, pid))
    for code in ('A', 'B', 'C'):
        empty_db_conn.execute(
            "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, ?, 'manual')", (g1, code))
    empty_db_conn.commit()
    _seed_book(book_conn, 'A', 'สินค้า 4 นิ้ว SS', stock=1, cost=1, vatcod='1')       # size only
    _seed_book(book_conn, 'B', 'สินค้า 4 นิ้ว AC', stock=1, cost=1, vatcod='1')       # size+color
    _seed_book(book_conn, 'C', 'สินค้า 6 นิ้ว AC', stock=1, cost=1, vatcod='1')       # color only
    cands = vs.get_candidates(pid, empty_db_conn, book_conn)
    assert [c['xp5_code'] for c in cands] == ['B', 'A', 'C']


# ── get_guesses (§5 cold-start) ─────────────────────────────────────────────

def test_guesses_stkgrp_bridge_when_identity_mapped(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
        "VALUES ('OWN', ?, 'reviewed', 'manual')", (pid,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'OWN', 'ของตัวเอง', stock=0, cost=0, stkgrp='57', vatcod='1')
    _seed_book(book_conn, 'G1', 'ของหมวดเดียวกัน', stock=2, cost=1, stkgrp='57', vatcod='1')
    _seed_book(book_conn, 'G2', 'ของหมวดอื่น', stock=2, cost=1, stkgrp='99', vatcod='1')
    result = vs.get_guesses(pid, empty_db_conn, book_conn, exclude_codes=set())
    assert result['empty'] is False
    codes = [r['xp5_code'] for r in result['items']]
    assert codes == ['G1']


def test_guesses_noun_prefix_when_no_identity(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn, name='กลอนเหล็ก#511-4นิ้ว AC')
    _seed_book(book_conn, 'G1', 'กลอนเหล็ก#520-6นิ้ว SS', stock=2, cost=1, vatcod='1')
    _seed_book(book_conn, 'G2', 'บานพับ#999-3นิ้ว AC', stock=2, cost=1, vatcod='1')
    result = vs.get_guesses(pid, empty_db_conn, book_conn, exclude_codes=set())
    assert result['empty'] is False
    codes = [r['xp5_code'] for r in result['items']]
    assert codes == ['G1']


def test_guesses_empty_state_when_name_has_no_noun(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn, name='#511-4นิ้ว AC')     # starts with #
    result = vs.get_guesses(pid, empty_db_conn, book_conn, exclude_codes=set())
    assert result['empty'] is True


def test_guesses_exclude_codes_already_in_pool(empty_db_conn, book_conn):
    pid = _seed_main_product(empty_db_conn, name='กลอนเหล็ก#511-4นิ้ว AC')
    _seed_book(book_conn, 'G1', 'กลอนเหล็ก#520-6นิ้ว SS', stock=2, cost=1, vatcod='1')
    result = vs.get_guesses(pid, empty_db_conn, book_conn, exclude_codes={'G1'})
    assert result['items'] == []


# ── get_unit_options ─────────────────────────────────────────────────────────

def test_unit_options_includes_base_and_conversions(empty_db_conn):
    pid = _seed_main_product(empty_db_conn, unit_type='ตัว')
    empty_db_conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)", (pid,))
    empty_db_conn.commit()
    opts = vs.get_unit_options(pid, empty_db_conn)
    by_unit = {o['unit']: o for o in opts}
    assert by_unit['ตัว'] == {'unit': 'ตัว', 'ratio': 1.0, 'is_base': True}
    assert by_unit['โหล'] == {'unit': 'โหล', 'ratio': 12.0, 'is_base': False}
