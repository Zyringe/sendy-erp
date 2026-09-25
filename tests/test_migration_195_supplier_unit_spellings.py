"""Migration 195 — GH #641 item 1: the supplier price list's own unit spellings.

Put decided seven of them on 2026-09-22 (decisions/log.md, option A): `เต้า` is a
`ลูก`, `กป.`/`ก.ป` are `กระป๋อง`, `กล.`/`ก.ล`/`ก.ล.` are `กล่อง`, `ปิ๊บ` is
`ปิ๊ป`, and `ปอนด์` stays a unit of its own. They go in under book `'*'` because
they are Sendy spellings a supplier typed, not Express codes.

The rule this has to meet (erp-engineering-discipline.md, the #609 blocker): a
migration that normalises stored values must write exactly what the IMPORTER
would write for the same raw input. That holds by construction here, and the
tests prove the construction rather than restating it: the supplier importer
already normalises through `bsn_units.normalize_unit(..., BOOK_ANY)`
(`scripts/import_supplier_catalogue.py`), the migration builds its translation
from `unit_map` AFTER the seed lands, and
`test_every_translated_row_holds_what_the_importer_produces` asks the runtime
normaliser about each one.

Two traps this pins, both from Put's own note on the decision:
  * `ขด` in a supplier column is a COIL of rope, not the Express code ขด = ขีด
    (64 rows on prod). A `'*'` row for it would silently turn all of them into
    100g, so the migration refuses if one exists.
  * `บักเต้า` is a product NAME. Every translation is an exact `unit = ?` match,
    so no product name can be touched — asserted, because a substring version of
    this migration would look identical in the diff.

Assert external behaviour: the word on a row, what the runtime map produces, what
the rollback restores.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import database

MIG = '195_supplier_unit_spellings.sql'
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG_195 = os.path.join(REPO, 'data', 'migrations', MIG)
ROLLBACK_195 = os.path.join(REPO, 'data', 'migrations', '195_supplier_unit_spellings.rollback.sql')

# Put's ruling, 2026-09-22. `ปอนด์` is deliberately absent.
DECIDED = {
    'เต้า': 'ลูก',
    'กป.': 'กระป๋อง',
    'ก.ป': 'กระป๋อง',
    'กล.': 'กล่อง',
    'ก.ล': 'กล่อง',
    'ก.ล.': 'กล่อง',
    'ปิ๊บ': 'ปิ๊ป',
}
TABLES = ('supplier_catalogue_items', 'supplier_catalogue_price_history')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def pre195_db(tmp_db):
    """The true pre-195 state: once the live dev DB has booted this branch,
    `tmp_db`'s clone already carries 195 and a bare init_db() would skip it."""
    conn = sqlite3.connect(tmp_db)
    try:
        applied = {r[0] for r in conn.execute("SELECT filename FROM applied_migrations")}
        if MIG in applied:
            conn.executescript(_read(ROLLBACK_195))
            conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def _migrate():
    database.init_db()


def _fk(conn):
    """A real supplier and catalogue version to hang test rows off. Both columns
    are NOT NULL and FK-constrained, so the rows have to be genuine."""
    sup = conn.execute("SELECT id FROM suppliers ORDER BY id LIMIT 1").fetchone()
    ver = conn.execute("SELECT id FROM supplier_catalogue_versions ORDER BY id LIMIT 1").fetchone()
    assert sup and ver, 'the clone has no supplier or catalogue version to attach to'
    return sup[0], ver[0]


def _item(conn, unit, name='supplier641'):
    """One supplier catalogue row plus its price-history row, both holding `unit`."""
    sup, ver = _fk(conn)
    sid = conn.execute(
        "INSERT INTO supplier_catalogue_items (supplier_id, name_raw, name_normalized, unit,"
        " is_active, created_at, updated_at)"
        " VALUES (?,?,?,?,1,'2026-09-25','2026-09-25')",
        (sup, name, name, unit)).lastrowid
    hid = conn.execute(
        "INSERT INTO supplier_catalogue_price_history (item_id, version_id, unit, captured_at)"
        " VALUES (?,?,?,'2026-09-25')",
        (sid, ver, unit)).lastrowid
    conn.commit()
    return sid, hid


def _units(conn, sid, hid):
    return (conn.execute("SELECT unit FROM supplier_catalogue_items WHERE id=?",
                         (sid,)).fetchone()[0],
            conn.execute("SELECT unit FROM supplier_catalogue_price_history WHERE id=?",
                         (hid,)).fetchone()[0])


# ── the map ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('spelling', sorted(DECIDED))
def test_the_map_learns_each_decided_spelling(pre195_db, spelling):
    _migrate()
    conn = _conn(pre195_db)
    try:
        row = conn.execute("SELECT book, word FROM unit_map WHERE spelling=?",
                           (spelling,)).fetchone()
        assert row is not None, f'{spelling} did not land in unit_map'
        assert (row['book'], row['word']) == ('*', DECIDED[spelling])
    finally:
        conn.close()


def test_pound_is_not_mapped(pre195_db):
    """Put ruled ปอนด์ stays a unit of its own."""
    _migrate()
    conn = _conn(pre195_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM unit_map WHERE spelling=?",
                            ('ปอนด์',)).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_coil_of_rope_is_not_mapped_book_independently(pre195_db):
    """`ขด` must keep meaning a coil in a supplier column. It maps to ขีด in the
    two Express books and must NOT gain a '*' row."""
    _migrate()
    conn = _conn(pre195_db)
    try:
        books = {r[0] for r in conn.execute("SELECT book FROM unit_map WHERE spelling=?",
                                           ('ขด',))}
        assert '*' not in books, books
    finally:
        conn.close()


def test_a_coil_in_a_supplier_row_survives(pre195_db):
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'ขด', 'supplier641 rope')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        assert _units(conn, sid, hid) == ('ขด', 'ขด')
    finally:
        conn.close()


# ── the translation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('spelling', sorted(DECIDED))
def test_each_stored_spelling_is_translated_in_both_tables(pre195_db, spelling):
    conn = _conn(pre195_db)
    sid, hid = _item(conn, spelling, f'supplier641 {spelling}')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        assert _units(conn, sid, hid) == (DECIDED[spelling], DECIDED[spelling])
    finally:
        conn.close()


@pytest.mark.parametrize('spelling', sorted(DECIDED))
def test_every_translated_row_holds_what_the_importer_produces(pre195_db, spelling):
    """The #609 rule, asked of the runtime normaliser rather than restated. The
    supplier importer reads the map with BOOK_ANY, so that is the book to ask."""
    import bsn_units

    conn = _conn(pre195_db)
    sid, hid = _item(conn, spelling, f'supplier641 importer {spelling}')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        stored = _units(conn, sid, hid)[0]
        assert stored == bsn_units.normalize_unit(spelling, bsn_units.BOOK_ANY, conn=conn)
    finally:
        conn.close()


def test_a_pound_row_is_left_alone(pre195_db):
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'ปอนด์', 'supplier641 pound')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        assert _units(conn, sid, hid) == ('ปอนด์', 'ปอนด์')
    finally:
        conn.close()


def test_a_product_name_containing_a_decided_word_is_untouched(pre195_db):
    """`บักเต้า` is a product name, not a unit. A substring version of this
    migration would look the same in the diff and would corrupt it."""
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'ตัว', 'บักเต้า 3 นิ้ว')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        assert conn.execute("SELECT name_raw FROM supplier_catalogue_items WHERE id=?",
                            (sid,)).fetchone()[0] == 'บักเต้า 3 นิ้ว'
        assert _units(conn, sid, hid) == ('ตัว', 'ตัว')
    finally:
        conn.close()


def test_a_unit_that_only_CONTAINS_a_decided_spelling_is_untouched(pre195_db):
    """Exact match, so `กล.เล็ก` (a unit of its own, mig 193) keeps its meaning
    even though it starts with `กล.`."""
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'กล.เล็ก', 'supplier641 small box')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        # 193 already maps กล.เล็ก -> กล่องเล็ก, and that is what it must become —
        # never กล่อง, which is what a substring translation would produce.
        assert _units(conn, sid, hid)[0] in ('กล.เล็ก', 'กล่องเล็ก')
        assert _units(conn, sid, hid)[0] != 'กล่อง'
    finally:
        conn.close()


def test_running_it_again_changes_nothing(pre195_db):
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'กป.', 'supplier641 rerun')
    conn.close()

    _migrate()
    conn = _conn(pre195_db)
    first = (_units(conn, sid, hid),
             conn.execute("SELECT COUNT(*) FROM migration_195_snapshot").fetchone()[0],
             sorted(tuple(r) for r in conn.execute("SELECT book, spelling, word FROM unit_map")))
    conn.close()

    conn = _conn(pre195_db)
    try:
        conn.executescript(_read(MIG_195))
        conn.commit()
        assert (_units(conn, sid, hid),
                conn.execute("SELECT COUNT(*) FROM migration_195_snapshot").fetchone()[0],
                sorted(tuple(r) for r in conn.execute("SELECT book, spelling, word FROM unit_map"))) == first
    finally:
        conn.close()


# ── preconditions ───────────────────────────────────────────────────────────

def test_it_refuses_when_a_spelling_already_means_something_else(pre195_db):
    conn = _conn(pre195_db)
    try:
        conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*','เต้า','ขวด')")
        conn.commit()
        with pytest.raises(sqlite3.Error) as exc:
            conn.executescript(_read(MIG_195))
        assert 'seed_conflict' in str(exc.value), str(exc.value)
        conn.rollback()
        assert conn.execute("SELECT word FROM unit_map WHERE spelling='เต้า'"
                            ).fetchone()[0] == 'ขวด'
    finally:
        conn.close()


def test_it_refuses_when_a_book_independent_coil_row_exists(pre195_db):
    conn = _conn(pre195_db)
    try:
        conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*','ขด','ขีด')")
        conn.commit()
        with pytest.raises(sqlite3.Error) as exc:
            conn.executescript(_read(MIG_195))
        assert 'kot_would_move' in str(exc.value), str(exc.value)
        conn.rollback()
    finally:
        conn.close()


def test_it_refuses_when_pound_has_been_mapped(pre195_db):
    conn = _conn(pre195_db)
    try:
        conn.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('*','ปอนด์','กิโลกรัม')")
        conn.commit()
        with pytest.raises(sqlite3.Error) as exc:
            conn.executescript(_read(MIG_195))
        assert 'pound_mapped' in str(exc.value), str(exc.value)
        conn.rollback()
    finally:
        conn.close()


# ── rollback ────────────────────────────────────────────────────────────────

def test_rollback_restores_the_labels_and_removes_the_map_rows(pre195_db):
    conn = _conn(pre195_db)
    rows = [_item(conn, s, f'supplier641 rb {s}') for s in sorted(DECIDED)]
    before_units = [_units(conn, sid, hid) for sid, hid in rows]
    before_map = sorted(tuple(r) for r in conn.execute("SELECT book, spelling, word FROM unit_map"))
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        conn.executescript(_read(ROLLBACK_195))
        conn.commit()
        assert [_units(conn, sid, hid) for sid, hid in rows] == before_units
        assert sorted(tuple(r) for r in conn.execute(
            "SELECT book, spelling, word FROM unit_map")) == before_map
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_195%'").fetchall()
    finally:
        conn.close()


def test_rollback_reports_a_row_edited_since(pre195_db):
    conn = _conn(pre195_db)
    sid, hid = _item(conn, 'กป.', 'supplier641 edited since')
    conn.close()

    _migrate()

    conn = _conn(pre195_db)
    try:
        conn.execute("UPDATE supplier_catalogue_items SET unit='ถัง' WHERE id=?", (sid,))
        conn.commit()
        conn.executescript(_read(ROLLBACK_195))
        skipped = [tuple(r) for r in conn.execute(
            "SELECT table_name, row_id, reason FROM temp.mig195_rollback_skipped")]
        assert ('supplier_catalogue_items', sid, 'changed after 195, left as is') in skipped, \
            skipped
        assert conn.execute("SELECT unit FROM supplier_catalogue_items WHERE id=?",
                            (sid,)).fetchone()[0] == 'ถัง'
        # its price-history twin was untouched, so that one IS restored
        assert conn.execute("SELECT unit FROM supplier_catalogue_price_history WHERE id=?",
                            (hid,)).fetchone()[0] == 'กป.'
    finally:
        conn.close()


def test_rollback_keeps_a_map_row_someone_repointed(pre195_db):
    _migrate()
    conn = _conn(pre195_db)
    try:
        conn.execute("UPDATE unit_map SET word='ถัง' WHERE book='*' AND spelling='เต้า'")
        conn.commit()
        conn.executescript(_read(ROLLBACK_195))
        assert conn.execute("SELECT word FROM unit_map WHERE book='*' AND spelling='เต้า'"
                            ).fetchone()[0] == 'ถัง'
        assert any('now says ถัง' in r[0] for r in conn.execute(
            "SELECT detail FROM temp.mig195_rollback_skipped"))
    finally:
        conn.close()
