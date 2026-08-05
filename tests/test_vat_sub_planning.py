"""vat-substitute — planning mode / all-groups view (plan §4.9): totals over
DISTINCT member codes passing the same eligibility filters as candidate
lists, sorted by total stock desc, zero-total groups visible at the bottom."""
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


def _seed_book(conn, code, name='ตัวแทน', unit='ตัว', stock=1.0, cost=1.0, stkgrp='', vatcod='1'):
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


def _seed_product(conn, name='สินค้า X'):
    cur = conn.execute("INSERT INTO products (product_name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def test_list_all_groups_totals_distinct_eligible_members(empty_db_conn, book_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลอนชุด A')").lastrowid
    pid = _seed_product(empty_db_conn)
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (gid, pid))
    for code in ('A', 'B'):
        empty_db_conn.execute(
            "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, ?, 'manual')", (gid, code))
    empty_db_conn.commit()
    _seed_book(book_conn, 'A', stock=3, vatcod='1')
    _seed_book(book_conn, 'B', stock=2, vatcod='0')     # excluded: VATCOD != '1'
    groups = vs.list_all_groups(empty_db_conn, book_conn)
    assert len(groups) == 1
    assert groups[0]['total_stock'] == 3
    assert groups[0]['member_count'] == 2               # membership count is raw, not eligibility-filtered
    assert groups[0]['product_count'] == 1


def test_list_all_groups_sorted_desc_and_zero_total_visible(empty_db_conn, book_conn):
    g_zero = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('ศูนย์')").lastrowid
    g_high = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('เยอะ')").lastrowid
    g_mid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('กลาง')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'H1', 'manual')", (g_high,))
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'M1', 'manual')", (g_mid,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'H1', stock=10, vatcod='1')
    _seed_book(book_conn, 'M1', stock=3, vatcod='1')
    groups = vs.list_all_groups(empty_db_conn, book_conn)
    assert [g['id'] for g in groups] == [g_high, g_mid, g_zero]
    assert groups[-1]['total_stock'] == 0


def test_list_all_groups_deduplicates_total_by_distinct_code(empty_db_conn, book_conn):
    """A code counted twice in overlapping groups still sums once PER GROUP
    (vat_sub_members has a UNIQUE(group_id, xp5_code) already) — this test
    pins that the total is a SUM over DISTINCT codes within the group, not
    accidentally doubled by a join fan-out against product_code_mapping."""
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'A', 'manual')", (gid,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'A', stock=7, vatcod='1')
    groups = vs.list_all_groups(empty_db_conn, book_conn)
    assert groups[0]['total_stock'] == 7


def test_get_group_detail_includes_members_and_linked_products(empty_db_conn, book_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    pid = _seed_product(empty_db_conn, name='สินค้าเชื่อม')
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (gid, pid))
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'A', 'manual')", (gid,))
    empty_db_conn.commit()
    _seed_book(book_conn, 'A', name='ของแทน A', stock=2, vatcod='1')
    detail = vs.get_group_detail(gid, empty_db_conn, book_conn)
    assert detail['label'] == 'g'
    assert detail['linked_products'][0]['name'] == 'สินค้าเชื่อม'
    assert detail['linked_products'][0]['id'] == pid
    assert detail['members'][0]['xp5_code'] == 'A'
    assert detail['members'][0]['added_from'] == 'manual'


def test_get_group_detail_none_when_group_missing(empty_db_conn, book_conn):
    assert vs.get_group_detail(99999, empty_db_conn, book_conn) is None


def test_group_members_include_stale_code_not_in_current_book(empty_db_conn, book_conn):
    """A member whose xp5_code has left the published book must still show
    up (so it can be removed from the group management page) — just with no
    book data and never counted toward total_stock."""
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'GONE', 'manual')", (gid,))
    empty_db_conn.commit()
    groups = vs.list_all_groups(empty_db_conn, book_conn)
    assert groups[0]['member_count'] == 1
    assert groups[0]['members'][0]['xp5_code'] == 'GONE'
    assert groups[0]['members'][0]['added_from'] == 'manual'
    assert groups[0]['total_stock'] == 0
