"""vat-substitute — sheet apply algorithm (plan §4.7): the executable
3-step deterministic targeting for rows marked "ใช้แทนกันได้" on the review
sheet. ONE transaction; idempotent; merge-free; independent re-verify with
rollback-on-mismatch."""
import sqlite3

import pytest

import models.vat_sub as vs


@pytest.fixture(autouse=True)
def _default_book(monkeypatch, tmp_path):
    """apply_substitution_sheet now validates Y against the published book
    (Codex r1 finding 3). The pre-existing algorithm tests in this file are
    about TARGETING/idempotency, not eligibility — give them a standard fake
    book carrying every code they use (all VATCOD='1') via open_vat_book,
    so a test that passes its own book_conn (the refusal tests below) is
    unaffected. Reopened per call: the function closes the conn it opens."""
    path = tmp_path / 'std_book.db'
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL, bsn_name TEXT NOT NULL,
            product_id INTEGER, bsn_unit TEXT NOT NULL DEFAULT '');
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE stmas_meta (
            stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, vatcod TEXT NOT NULL);
    """)
    for code in ('Y1', 'Y2', 'A', 'B', 'NEW', 'OLD'):
        pid = c.execute("INSERT INTO products (product_name) VALUES (?)",
                        (f'ตัวแทน {code}',)).lastrowid
        c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
                  "VALUES (?, ?, ?)", (code, f'ตัวแทน {code}', pid))
        c.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 1.0)", (pid,))
        c.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES (?, '', '1')", (code,))
    c.commit()
    c.close()

    def _open():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(vs, 'open_vat_book', _open)


def _seed_product(conn, name='สินค้า X'):
    cur = conn.execute("INSERT INTO products (product_name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _counts(conn):
    return {
        'groups': conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0],
        'members': conn.execute("SELECT COUNT(*) FROM vat_sub_members").fetchone()[0],
        'links': conn.execute("SELECT COUNT(*) FROM vat_sub_product_links").fetchone()[0],
    }


def _resident(conn, product_id, xp5_code):
    return conn.execute("""
        SELECT l.group_id FROM vat_sub_product_links l
        JOIN vat_sub_members m ON m.group_id = l.group_id
        WHERE l.product_id = ? AND m.xp5_code = ?
    """, (product_id, xp5_code)).fetchall()


def test_cold_start_creates_group_labeled_from_category_noun(empty_db_conn):
    pid = _seed_product(empty_db_conn, name='กลอนเหล็ก#511-4นิ้ว AC')
    result = vs.apply_substitution_sheet(
        [{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    assert result['ok'] is True
    assert result['applied'] == 1
    rows = _resident(empty_db_conn, pid, 'Y1')
    assert len(rows) == 1
    gid = rows[0]['group_id']
    label = empty_db_conn.execute("SELECT label FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()['label']
    assert label == 'กลอนเหล็ก'
    member = empty_db_conn.execute(
        "SELECT added_from FROM vat_sub_members WHERE group_id=? AND xp5_code='Y1'", (gid,)).fetchone()
    assert member['added_from'] == 'sheet'


def test_step1_x_already_has_group_adds_y_to_lowest(empty_db_conn):
    pid = _seed_product(empty_db_conn)
    g_low = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('a')").lastrowid
    g_high = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('b')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (max(g_low, g_high), pid))
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (min(g_low, g_high), pid))
    empty_db_conn.commit()
    result = vs.apply_substitution_sheet([{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    assert result['ok'] is True
    member = empty_db_conn.execute(
        "SELECT group_id FROM vat_sub_members WHERE xp5_code='Y1'").fetchone()
    assert member['group_id'] == min(g_low, g_high)


def test_step2_y_already_member_links_x_to_lowest(empty_db_conn):
    pid = _seed_product(empty_db_conn)
    g_low = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('a')").lastrowid
    g_high = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('b')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (max(g_low, g_high),))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (min(g_low, g_high),))
    empty_db_conn.commit()
    result = vs.apply_substitution_sheet([{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    assert result['ok'] is True
    link = empty_db_conn.execute(
        "SELECT group_id FROM vat_sub_product_links WHERE product_id=?", (pid,)).fetchone()
    assert link['group_id'] == min(g_low, g_high)


def test_bridging_row_is_merge_free_y_ends_up_in_both_groups(empty_db_conn):
    """§4.7: existing groups are NEVER combined. A row bridging X's group
    and Y's group resolves at step 1 (X already has a group -> add Y there)
    — both groups survive, Y holding membership in both is decision 11's
    feature, not a bug."""
    pid = _seed_product(empty_db_conn)
    g_x = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('x-group')").lastrowid
    g_y = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('y-group')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (g_x, pid))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g_y,))
    empty_db_conn.commit()
    before_groups = empty_db_conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0]
    result = vs.apply_substitution_sheet([{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    assert result['ok'] is True
    after_groups = empty_db_conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0]
    assert after_groups == before_groups                 # no merge, no new group
    # Y is now a member of g_x too (added by the sheet), STILL a member of g_y (untouched)
    y_groups = {r['group_id'] for r in empty_db_conn.execute(
        "SELECT group_id FROM vat_sub_members WHERE xp5_code='Y1'")}
    assert y_groups == {g_x, g_y}


def test_idempotent_rerun_no_duplicates(empty_db_conn):
    pid = _seed_product(empty_db_conn)
    rows = [{'product_id': pid, 'xp5_code': 'Y1'}]
    first = vs.apply_substitution_sheet(rows, conn=empty_db_conn)
    counts_after_first = _counts(empty_db_conn)
    second = vs.apply_substitution_sheet(rows, conn=empty_db_conn)
    assert first['ok'] is True and second['ok'] is True
    assert _counts(empty_db_conn) == counts_after_first   # re-run is a pure no-op


def test_rerun_is_label_stable(empty_db_conn):
    """Labels are set ONLY at creation, never touched on re-run (plan §4.7
    'rename-stable')."""
    pid = _seed_product(empty_db_conn, name='กลอนเหล็ก#511-4นิ้ว AC')
    vs.apply_substitution_sheet([{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    gid = empty_db_conn.execute(
        "SELECT group_id FROM vat_sub_product_links WHERE product_id=?", (pid,)).fetchone()['group_id']
    empty_db_conn.execute("UPDATE vat_sub_groups SET label='ชื่อที่ Put เปลี่ยนเอง' WHERE id=?", (gid,))
    empty_db_conn.commit()
    vs.apply_substitution_sheet([{'product_id': pid, 'xp5_code': 'Y1'}], conn=empty_db_conn)
    label = empty_db_conn.execute("SELECT label FROM vat_sub_groups WHERE id=?", (gid,)).fetchone()['label']
    assert label == 'ชื่อที่ Put เปลี่ยนเอง'


def test_additive_only_never_removes_existing_links_or_members(empty_db_conn):
    pid1 = _seed_product(empty_db_conn, name='สินค้า 1')
    pid2 = _seed_product(empty_db_conn, name='สินค้า 2')
    gid = empty_db_conn.execute("INSERT INTO vat_sub_groups (label) VALUES ('g')").lastrowid
    empty_db_conn.execute("INSERT INTO vat_sub_product_links (group_id, product_id) VALUES (?, ?)", (gid, pid1))
    empty_db_conn.execute("INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'OLD', 'manual')", (gid,))
    empty_db_conn.commit()
    vs.apply_substitution_sheet([{'product_id': pid2, 'xp5_code': 'NEW'}], conn=empty_db_conn)
    # pid1/OLD from before the run must still be exactly as they were
    assert empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_product_links WHERE group_id=? AND product_id=?", (gid, pid1)).fetchone()
    assert empty_db_conn.execute(
        "SELECT 1 FROM vat_sub_members WHERE group_id=? AND xp5_code='OLD'", (gid,)).fetchone()


def test_deterministic_order_processes_by_product_id_then_xp5_code(empty_db_conn):
    """Rows fed out of order still resolve identically — the function sorts
    internally (plan §4.7: ORDER BY product_id, xp5_code). Compares
    STRUCTURE (both codes land in exactly one shared group), not raw
    autoincrement ids, which legitimately differ across two separate runs."""
    pid = _seed_product(empty_db_conn)
    rows_forward = [{'product_id': pid, 'xp5_code': 'B'}, {'product_id': pid, 'xp5_code': 'A'}]
    rows_reverse = list(reversed(rows_forward))
    vs.apply_substitution_sheet(rows_forward, conn=empty_db_conn)
    groups_a = {r['group_id'] for r in empty_db_conn.execute("SELECT group_id FROM vat_sub_members")}
    assert len(groups_a) == 1                             # both codes share ONE group

    empty_db_conn.execute("DELETE FROM vat_sub_members")
    empty_db_conn.execute("DELETE FROM vat_sub_product_links")
    empty_db_conn.execute("DELETE FROM vat_sub_groups")
    empty_db_conn.commit()

    vs.apply_substitution_sheet(rows_reverse, conn=empty_db_conn)
    groups_b = {r['group_id'] for r in empty_db_conn.execute("SELECT group_id FROM vat_sub_members")}
    assert len(groups_b) == 1


class _SpyConn:
    """sqlite3.Connection disallows setting arbitrary attributes (its
    `execute` is a read-only C-level method), so a monkeypatched instance
    attribute can't shadow it — wrap it in a duck-typed proxy instead. The
    function under test only ever calls execute/commit/rollback."""
    def __init__(self, real, skip_sql_substring, skip_after_n=0):
        self._real = real
        self._skip = skip_sql_substring
        self._n = skip_after_n
        self._hits = 0

    def execute(self, sql, params=()):
        if self._skip in sql and self._hits == self._n:
            self._hits += 1
            return self._real.execute("SELECT 1")   # silently skip the real write
        if self._skip in sql:
            self._hits += 1
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()


def test_verify_mismatch_rolls_back_entire_transaction(empty_db_conn):
    """A fault that leaves even ONE processed pair not co-resident must roll
    back EVERYTHING, not just the bad row (plan §4.7)."""
    pid1 = _seed_product(empty_db_conn, name='สินค้า 1')
    pid2 = _seed_product(empty_db_conn, name='สินค้า 2')

    spy = _SpyConn(empty_db_conn, "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
                   "VALUES (?, ?, 'sheet')")
    result = vs.apply_substitution_sheet(
        [{'product_id': pid1, 'xp5_code': 'Y1'}, {'product_id': pid2, 'xp5_code': 'Y2'}],
        conn=spy)
    assert result['ok'] is False
    assert (pid1, 'Y1') in result['mismatches']
    # Y2's pair (which WAS written correctly) must also be gone — whole-txn rollback
    assert empty_db_conn.execute("SELECT COUNT(*) FROM vat_sub_groups").fetchone()[0] == 0
    assert empty_db_conn.execute("SELECT COUNT(*) FROM vat_sub_members").fetchone()[0] == 0


# ── Codex r1 finding 3: batch validation (X active, Y in current book with
# VATCOD='1') BEFORE the first write — a stale/edited CSV must never seed
# orphan or book-invalid curation rows. Whole-batch refusal, zero writes. ──
import sqlite3
import pytest


@pytest.fixture
def sheet_book(tmp_path):
    c = sqlite3.connect(tmp_path / 'sheet_book.db')
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            unit_type TEXT NOT NULL DEFAULT 'ตัว',
            cost_price REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE product_code_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bsn_code TEXT NOT NULL, bsn_name TEXT NOT NULL,
            product_id INTEGER, bsn_unit TEXT NOT NULL DEFAULT '');
        CREATE TABLE stock_levels (
            product_id INTEGER PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE stmas_meta (
            stkcod TEXT PRIMARY KEY, stkgrp TEXT NOT NULL, vatcod TEXT NOT NULL);
    """)
    yield c
    c.close()


def _seed_sheet_book(conn, code, vatcod='1'):
    pid = conn.execute(
        "INSERT INTO products (product_name) VALUES (?)", (f'ตัวแทน {code}',)).lastrowid
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
                 "VALUES (?, ?, ?)", (code, f'ตัวแทน {code}', pid))
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 1.0)", (pid,))
    conn.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES (?, '', ?)",
                 (code, vatcod))
    conn.commit()


def test_apply_refuses_inactive_x_whole_batch(empty_db_conn, sheet_book):
    ok_pid = _seed_product(empty_db_conn, name='สินค้าดี')
    bad_pid = empty_db_conn.execute(
        "INSERT INTO products (product_name, is_active) VALUES ('ปิดใช้งานแล้ว', 0)").lastrowid
    empty_db_conn.commit()
    for code in ('Y1', 'Y2'):
        _seed_sheet_book(sheet_book, code)
    result = vs.apply_substitution_sheet(
        [{'product_id': ok_pid, 'xp5_code': 'Y1'},
         {'product_id': bad_pid, 'xp5_code': 'Y2'}],
        conn=empty_db_conn, book_conn=sheet_book)
    assert result['ok'] is False
    assert any(pid == bad_pid for pid, _code, _why in result['invalid'])
    assert _counts(empty_db_conn) == {'groups': 0, 'members': 0, 'links': 0}


def test_apply_refuses_y_not_in_current_book(empty_db_conn, sheet_book):
    pid = _seed_product(empty_db_conn)
    _seed_sheet_book(sheet_book, 'Y1')
    result = vs.apply_substitution_sheet(
        [{'product_id': pid, 'xp5_code': 'Y1'},
         {'product_id': pid, 'xp5_code': 'GONE'}],
        conn=empty_db_conn, book_conn=sheet_book)
    assert result['ok'] is False
    assert any(code == 'GONE' for _pid, code, _why in result['invalid'])
    assert _counts(empty_db_conn) == {'groups': 0, 'members': 0, 'links': 0}


def test_apply_refuses_y_vatcod_not_1(empty_db_conn, sheet_book):
    pid = _seed_product(empty_db_conn)
    _seed_sheet_book(sheet_book, 'Y0', vatcod='0')
    result = vs.apply_substitution_sheet(
        [{'product_id': pid, 'xp5_code': 'Y0'}],
        conn=empty_db_conn, book_conn=sheet_book)
    assert result['ok'] is False
    assert any(code == 'Y0' for _pid, code, _why in result['invalid'])
    assert _counts(empty_db_conn) == {'groups': 0, 'members': 0, 'links': 0}
