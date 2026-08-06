"""vat-substitute — curation writes: promote, add-member, move-member,
remove-member, link-product, unlink-product, rename-group, delete-group
(plan §4.5/§4.6/§5). Every write: ONE BEGIN IMMEDIATE transaction, refusal
checks BEFORE any mutation, idempotent outcomes defined explicitly.

book_conn is injected explicitly (dependency injection, same conn=None
pattern as the rest of the app) so these never touch the real vat_book.db
file in tests."""
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


def _seed_product(conn, name='สินค้า X', is_active=1):
    cur = conn.execute("INSERT INTO products (product_name, is_active) VALUES (?, ?)", (name, is_active))
    conn.commit()
    return cur.lastrowid


def _counts(conn):
    return {
        'groups': conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0],
        'members': conn.execute("SELECT COUNT(*) FROM vat_sub_members").fetchone()[0],
        'links': conn.execute("SELECT COUNT(*) FROM vat_sub_product_links").fetchone()[0],
    }


# ── promote (§4.5) ───────────────────────────────────────────────────────────

def test_promote_cold_start_creates_group_links_and_member(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn, name='กลอนเหล็ก#511-4นิ้ว AC')
    _seed_book(book_conn, 'Y1', vatcod='1')
    result = vs.promote(pid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True
    assert result['created_group'] is True
    gid = result['group_id']
    grp = empty_db_conn.execute("SELECT label FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()
    assert grp['label'] == 'กลอนเหล็ก'
    assert empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?", (gid, pid)).fetchone()
    member = empty_db_conn.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (gid,)).fetchone()
    assert member['added_from'] == 'promote'


def test_promote_defaults_to_lowest_existing_group(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1')
    _seed_book(book_conn, 'Y2')
    g_low = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('a')").lastrowid
    g_high = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('b')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (max(g_low, g_high), pid))
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (min(g_low, g_high), pid))
    empty_db_conn.commit()
    result = vs.promote(pid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True
    assert result['group_id'] == min(g_low, g_high)
    assert result['created_group'] is False


def test_promote_explicit_target_group_links_x_if_not_already(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1')
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    result = vs.promote(pid, 'Y1', target_group_id=gid, conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True
    assert result['group_id'] == gid
    assert empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?", (gid, pid)).fetchone()


def test_promote_force_new_group_even_when_x_has_groups(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1')
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g1, pid))
    empty_db_conn.commit()
    result = vs.promote(pid, 'Y1', target_group_id='new', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True
    assert result['created_group'] is True
    assert result['group_id'] != g1


def test_promote_repeat_is_noop(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1')
    first = vs.promote(pid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    before = _counts(empty_db_conn)
    second = vs.promote(pid, 'Y1', target_group_id=first['group_id'], conn=empty_db_conn, book_conn=book_conn)
    assert second['ok'] is True
    assert second['noop'] is True
    assert _counts(empty_db_conn) == before


def test_promote_refuses_inactive_product_writes_nothing(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn, is_active=0)
    _seed_book(book_conn, 'Y1')
    before = _counts(empty_db_conn)
    result = vs.promote(pid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False
    assert _counts(empty_db_conn) == before


def test_promote_refuses_unknown_target_group_writes_nothing(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1')
    before = _counts(empty_db_conn)
    result = vs.promote(pid, 'Y1', target_group_id=99999, conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False
    assert _counts(empty_db_conn) == before


def test_promote_refuses_y_not_in_book_writes_nothing(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    before = _counts(empty_db_conn)
    result = vs.promote(pid, 'NOPE', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False
    assert _counts(empty_db_conn) == before


def test_promote_refuses_y_vatcod_not_1_writes_nothing(empty_db_conn, book_conn):
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'Y1', vatcod='0')
    before = _counts(empty_db_conn)
    result = vs.promote(pid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False
    assert _counts(empty_db_conn) == before


# ── add_member ───────────────────────────────────────────────────────────────

def test_add_member_success(empty_db_conn, book_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    _seed_book(book_conn, 'Y1')
    result = vs.add_member(gid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True
    member = empty_db_conn.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (gid,)).fetchone()
    assert member['added_from'] == 'manual'


def test_add_member_refuses_unknown_group(empty_db_conn, book_conn):
    _seed_book(book_conn, 'Y1')
    result = vs.add_member(99999, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False


def test_add_member_refuses_vatcod_not_1(empty_db_conn, book_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    _seed_book(book_conn, 'Y1', vatcod='0')
    result = vs.add_member(gid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is False


def test_add_member_zero_stock_code_allowed(empty_db_conn, book_conn):
    """§4.7: add-member accepts any code still present in the current book
    with VATCOD='1' — zero-stock included (only the guess section's >= 0.5
    filter hides it, not the write path)."""
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    _seed_book(book_conn, 'Y1', stock=0)
    result = vs.add_member(gid, 'Y1', conn=empty_db_conn, book_conn=book_conn)
    assert result['ok'] is True


# ── move_member ──────────────────────────────────────────────────────────────

def test_move_member_source_only_moves_and_preserves_added_from(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'sheet')", (g1,))
    empty_db_conn.commit()
    result = vs.move_member(g1, g2, 'Y1', conn=empty_db_conn)
    assert result['ok'] is True
    assert not empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (g1,)).fetchone()
    row = empty_db_conn.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (g2,)).fetchone()
    assert row['added_from'] == 'sheet'


def test_move_member_both_present_keeps_target_deletes_source(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'sheet')", (g1,))
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g2,))
    empty_db_conn.commit()
    result = vs.move_member(g1, g2, 'Y1', conn=empty_db_conn)
    assert result['ok'] is True
    assert not empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (g1,)).fetchone()
    row = empty_db_conn.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (g2,)).fetchone()
    assert row['added_from'] == 'manual'   # target's own provenance untouched


def test_move_member_target_only_is_friendly_noop(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g2,))
    empty_db_conn.commit()
    result = vs.move_member(g1, g2, 'Y1', conn=empty_db_conn)
    assert result['ok'] is True
    assert result.get('noop') is True


def test_move_member_neither_present_refuses(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    empty_db_conn.commit()
    result = vs.move_member(g1, g2, 'Y1', conn=empty_db_conn)
    assert result['ok'] is False


def test_move_member_refuses_same_source_and_target(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.commit()
    result = vs.move_member(g1, g1, 'Y1', conn=empty_db_conn)
    assert result['ok'] is False


def test_move_member_refuses_unknown_target_group(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g1,))
    empty_db_conn.commit()
    result = vs.move_member(g1, 99999, 'Y1', conn=empty_db_conn)
    assert result['ok'] is False
    assert empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (g1,)).fetchone()


# ── remove_member / link_product / unlink_product ───────────────────────────

def test_remove_member_deletes_and_is_idempotent(empty_db_conn):
    g1 = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g1,))
    empty_db_conn.commit()
    r1 = vs.remove_member(g1, 'Y1', conn=empty_db_conn)
    assert r1['ok'] is True and r1['noop'] is False
    r2 = vs.remove_member(g1, 'Y1', conn=empty_db_conn)
    assert r2['ok'] is True and r2['noop'] is True


def test_link_product_success_and_refuses_inactive(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    active_pid = _seed_product(empty_db_conn, is_active=1)
    inactive_pid = _seed_product(empty_db_conn, is_active=0)
    ok = vs.link_product(gid, active_pid, conn=empty_db_conn)
    assert ok['ok'] is True
    bad = vs.link_product(gid, inactive_pid, conn=empty_db_conn)
    assert bad['ok'] is False
    assert not empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?", (gid, inactive_pid)).fetchone()


def test_unlink_product_deletes_and_is_idempotent(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    pid = _seed_product(empty_db_conn)
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (gid, pid))
    empty_db_conn.commit()
    r1 = vs.unlink_product(gid, pid, conn=empty_db_conn)
    assert r1['ok'] is True and r1['noop'] is False
    r2 = vs.unlink_product(gid, pid, conn=empty_db_conn)
    assert r2['ok'] is True and r2['noop'] is True


# ── rename_group / delete_group ─────────────────────────────────────────────

def test_rename_group_updates_label(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('เดิม')").lastrowid
    empty_db_conn.commit()
    result = vs.rename_group(gid, 'ใหม่', conn=empty_db_conn)
    assert result['ok'] is True
    assert empty_db_conn.execute("SELECT label FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()['label'] == 'ใหม่'


def test_rename_group_refuses_blank_label(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('เดิม')").lastrowid
    empty_db_conn.commit()
    result = vs.rename_group(gid, '   ', conn=empty_db_conn)
    assert result['ok'] is False
    assert empty_db_conn.execute("SELECT label FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()['label'] == 'เดิม'


def test_delete_empty_group_succeeds(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.commit()
    result = vs.delete_group(gid, conn=empty_db_conn)
    assert result['ok'] is True
    assert not empty_db_conn.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()


def test_delete_nonempty_group_refuses(empty_db_conn):
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (gid,))
    empty_db_conn.commit()
    result = vs.delete_group(gid, conn=empty_db_conn)
    assert result['ok'] is False
    assert empty_db_conn.execute("SELECT 1 FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()


def test_delete_already_gone_group_is_idempotent_noop(empty_db_conn):
    result = vs.delete_group(99999, conn=empty_db_conn)
    assert result['ok'] is True
    assert result.get('noop') is True


def test_write_busy_timeout_returns_busy_result_not_500(empty_db_conn, book_conn, monkeypatch):
    """Codex r1 finding 2 (§4.6): a writer lock that outlives busy_timeout
    raises sqlite3.OperationalError('database is locked') — the model must
    convert that into the standard busy result so every POST route flashes
    "ระบบกำลังยุ่ง ลองใหม่อีกครั้ง" + redirect instead of a 500."""
    pid = _seed_product(empty_db_conn)
    _seed_book(book_conn, 'YBUSY')

    def boom(*_a, **_k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(vs, '_x_group_ids', boom)

    result = vs.promote(pid, 'YBUSY', conn=empty_db_conn, book_conn=book_conn)
    assert result == {'ok': False, 'error': 'ระบบกำลังยุ่ง ลองใหม่อีกครั้ง'}
    assert empty_db_conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0] == 0
