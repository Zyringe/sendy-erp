"""vat-substitute — concurrency seam BEFORE the first write (plan §4.6).

Under the default deferred isolation a connection holds no lock until it
writes, so a probe injected AFTER the first write would be excluded either
way — with or without BEGIN IMMEDIATE — and prove nothing (see
.claude/rules/erp-engineering-discipline.md, "A concurrency test whose seam
sits AFTER the first write proves nothing"). Both tests here patch a helper
called strictly BEFORE any mutation (_x_group_ids for promote's cold-start
create-vs-add decision; _member_state for move_member's four-state branch),
open a SECOND connection with a short timeout, and assert it is excluded.
Uses a real file-based DB (tmp_db) — sqlite locking is a filesystem-level
property, not observable against :memory:."""
import sqlite3

import pytest

import models.vat_sub as vs


def _book_conn(tmp_path, code='Y1', vatcod='1', stock=1.0):
    c = sqlite3.connect(tmp_path / 'concurrency_book.db')
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
    pid = c.execute(
        "INSERT INTO products (product_name) VALUES ('ตัวแทน')").lastrowid
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
             "VALUES (?, 'ตัวแทน', ?)", (code, pid))
    c.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, ?)", (pid, stock))
    c.execute("INSERT INTO stmas_meta (stkcod, stkgrp, vatcod) VALUES (?, '', ?)", (code, vatcod))
    c.commit()
    return c


def test_promote_holds_lock_before_first_write(tmp_db, tmp_path, monkeypatch):
    """The cold-start create-vs-add decision (plan §4.6) must be made INSIDE
    the write lock: two simultaneous cold-start promotes must serialize on
    SQLite's single writer. _x_group_ids is the last read before that
    decision — patch it, probe from a second connection, assert excluded."""
    import config
    conn0 = sqlite3.connect(config.DATABASE_PATH)
    conn0.row_factory = sqlite3.Row
    conn0.execute("PRAGMA foreign_keys = ON")
    pid = conn0.execute(
        "INSERT INTO products (product_name) VALUES ('สินค้าทดสอบ concurrency')").lastrowid
    conn0.commit()
    conn0.close()

    book_conn = _book_conn(tmp_path)
    seen = {}
    real_fn = vs._x_group_ids

    def _spy(c, product_id):
        probe = sqlite3.connect(config.DATABASE_PATH, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                probe.execute("INSERT INTO vat_sub_groups (label) VALUES ('intruder')")
            seen['locked'] = True
        finally:
            probe.close()
        return real_fn(c, product_id)

    monkeypatch.setattr(vs, '_x_group_ids', _spy)
    result = vs.promote(pid, 'Y1', book_conn=book_conn)
    book_conn.close()

    assert seen.get('locked') is True
    assert result == {'ok': True, 'group_id': result['group_id'],
                      'created_group': True, 'noop': False}


def test_move_member_holds_lock_before_first_write(tmp_db, monkeypatch):
    """_member_state is the last read before move_member's four-state
    branch (source-only/both/target-only/neither) — same seam requirement."""
    import config
    conn0 = sqlite3.connect(config.DATABASE_PATH)
    conn0.row_factory = sqlite3.Row
    conn0.execute("PRAGMA foreign_keys = ON")
    g1 = conn0.execute("INSERT INTO vat_sub_groups (label) VALUES ('g1')").lastrowid
    g2 = conn0.execute("INSERT INTO vat_sub_groups (label) VALUES ('g2')").lastrowid
    conn0.execute(
        "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) VALUES (?, 'Y1', 'manual')", (g1,))
    conn0.commit()
    conn0.close()

    seen = {}
    real_fn = vs._member_state

    def _spy(c, source_group_id, target_group_id, xp5_code):
        probe = sqlite3.connect(config.DATABASE_PATH, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                probe.execute(
                    "INSERT INTO vat_sub_members (group_id, xp5_code, added_from) "
                    "VALUES (?, 'INTRUDER', 'manual')", (target_group_id,))
            seen['locked'] = True
        finally:
            probe.close()
        return real_fn(c, source_group_id, target_group_id, xp5_code)

    monkeypatch.setattr(vs, '_member_state', _spy)
    result = vs.move_member(g1, g2, 'Y1')

    assert seen.get('locked') is True
    assert result == {'ok': True}
