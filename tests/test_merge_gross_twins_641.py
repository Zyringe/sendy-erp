"""scripts/2026_09_25_merge_gross_twins_641.py — #641 item 3.

The last Express short code in `unit_conversions.bsn_unit` is `กร`, and on prod
every one of its 12 rows duplicates a `กุรุส` row on the same product at the same
ratio. This drops the duplicate and leaves the twin.

SELECTION IS THE GUARD, not a refusal. A lone `กร` row, or a pair that disagrees
on ratio, is a different operation and is reported and left alone. That matters
concretely: the first version of this work was migration 195, whose whole-table
precondition aborted `database.init_db()` and so reached the fixtures of the
migration suites that run before it — 193's own fixture even carries the comment
"# would abort as twin_ratio". 8 to 10 tests in three other files went red,
every one of them caused by that change (baseline without it: 361 passed, 0
failed). As a script it runs when a person runs it, and these tests never call
`init_db()`, so nothing else in the suite can be disturbed either way.

What DOES refuse is scoped to the merged products: a bill line reading `กร` on
one of them (the #609 hazard), a missing `unit_map` row, or a twin whose ratio
moved between the two reads.

Assert external behaviour: the rows on a product, what a re-import does, what the
rehearsal leaves behind.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3

import pytest

from tests.test_migration_186_unit_code_cleanup import (
    _entry, _new_product, _purchase, _sale, _uc, _units)

_SCRIPT = str(pathlib.Path(__file__).resolve().parents[1]
              / 'scripts' / '2026_09_25_merge_gross_twins_641.py')
_spec = importlib.util.spec_from_file_location('merge_gross_twins_641', _SCRIPT)
merge_twins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_twins)

CODE = merge_twins.CODE
WORD = merge_twins.WORD
GROSS = 144.0


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture
def db(tmp_db):
    """A clone with NO `กร` rows, so every test states its own case. The live
    snapshot carries the real twelve, and inheriting them would make every count
    below depend on prod data."""
    conn = _conn(tmp_db)
    conn.execute("DELETE FROM unit_conversions WHERE bsn_unit = ?", (CODE,))
    conn.commit()
    conn.close()
    return tmp_db


def _twin(conn, name, code_ratio=GROSS, word_ratio=GROSS, unit_type='ตัว'):
    pid = _new_product(conn, name, unit_type=unit_type)
    _uc(conn, pid, CODE, code_ratio)
    _uc(conn, pid, WORD, word_ratio)
    conn.commit()
    return pid


def _run(db_path, apply=True):
    conn = _conn(db_path)
    try:
        return merge_twins.run(conn, apply)
    finally:
        conn.close()


# ── the merge ───────────────────────────────────────────────────────────────

def test_the_duplicate_goes_and_the_twin_stays(db):
    conn = _conn(db)
    pid = _twin(conn, 'twins641 duplicate')
    conn.close()

    code, report = _run(db)

    assert code == 0 and report['applied'], report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {WORD: GROSS}
    finally:
        conn.close()
    assert [d['product_id'] for d in report['merge']] == [pid], report


def test_a_rehearsal_writes_nothing(db):
    conn = _conn(db)
    pid = _twin(conn, 'twins641 rehearsal')
    conn.close()

    code, report = _run(db, apply=False)

    assert code == 0 and report['applied'] is False, report
    assert [d['product_id'] for d in report['merge']] == [pid]
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS, WORD: GROSS}
    finally:
        conn.close()


def test_running_it_again_finds_nothing(db):
    conn = _conn(db)
    _twin(conn, 'twins641 rerun')
    conn.close()

    assert _run(db)[0] == 0
    code, report = _run(db)
    assert code == 0 and report['merge'] == [] and report['note'] == 'nothing to merge', report


def test_ratio_stock_cost_and_ledger_do_not_move(db):
    conn = _conn(db)
    pid = _twin(conn, 'twins641 ledger')
    _sale(conn, 'IV6410-1', pid, WORD, qty=2, price=960)
    conn.commit()
    before = (conn.execute("SELECT cost_price, opening_cost, base_sell_price FROM products "
                           "WHERE id=?", (pid,)).fetchone()[:],
              conn.execute("SELECT COUNT(*), COALESCE(SUM(quantity_change),0) FROM transactions"
                           ).fetchone()[:])
    conn.close()

    assert _run(db)[0] == 0

    conn = _conn(db)
    try:
        assert (conn.execute("SELECT cost_price, opening_cost, base_sell_price FROM products "
                             "WHERE id=?", (pid,)).fetchone()[:],
                conn.execute("SELECT COUNT(*), COALESCE(SUM(quantity_change),0) FROM transactions"
                             ).fetchone()[:]) == before
    finally:
        conn.close()


# ── selection: what it refuses to treat as a duplicate ──────────────────────

def test_a_lone_code_row_is_left_alone_and_reported(db):
    conn = _conn(db)
    pid = _new_product(conn, 'twins641 lone')
    _uc(conn, pid, CODE, GROSS)
    conn.commit()
    conn.close()

    code, report = _run(db)

    assert code == 0 and report['merge'] == [], report
    assert any(s['product_id'] == pid and 'not a duplicate' in s['reason']
               for s in report['skipped']), report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS}
    finally:
        conn.close()


def test_a_disagreeing_pair_is_left_alone_and_reported(db):
    conn = _conn(db)
    pid = _twin(conn, 'twins641 clash', code_ratio=GROSS, word_ratio=12.0)
    conn.close()

    code, report = _run(db)

    assert code == 0 and report['merge'] == [], report
    assert any(s['product_id'] == pid and 'needs a ruling' in s['reason']
               for s in report['skipped']), report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS, WORD: 12.0}
    finally:
        conn.close()


def test_a_row_it_will_not_touch_cannot_block_a_real_duplicate(db):
    """The regression migration 195 had: a whole-table check meant one lone row
    anywhere stopped the whole thing."""
    conn = _conn(db)
    lone = _new_product(conn, 'twins641 bystander lone')
    _uc(conn, lone, CODE, GROSS)
    clash = _twin(conn, 'twins641 bystander clash', code_ratio=GROSS, word_ratio=1.0)
    dup = _twin(conn, 'twins641 real duplicate')
    conn.close()

    code, report = _run(db)

    assert code == 0, report
    conn = _conn(db)
    try:
        assert _units(conn, dup) == {WORD: GROSS}, 'the duplicate should have merged'
        assert _units(conn, lone) == {CODE: GROSS}
        assert _units(conn, clash) == {CODE: GROSS, WORD: 1.0}
    finally:
        conn.close()


# ── preconditions: refuse, and write nothing ────────────────────────────────

@pytest.mark.parametrize('table', ['sale', 'purchase'])
def test_it_refuses_when_a_bill_line_still_reads_the_code(db, table):
    """#609: drop the conversion a stored line depends on and the next import
    finds it unsyncable, rewrites it and moves stock."""
    conn = _conn(db)
    pid = _twin(conn, f'twins641 in use {table}')
    if table == 'sale':
        _sale(conn, 'IV6411-1', pid, CODE)
    else:
        _purchase(conn, 'HP6411', pid, CODE)
    conn.commit()
    conn.close()

    code, report = _run(db)

    assert code == 2, report
    assert any('#609' in p for p in report['problems']), report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS, WORD: GROSS}
    finally:
        conn.close()


def test_a_bill_line_on_another_product_does_not_block(db):
    """Pins the SCOPING of that refusal. A line on a product this script is not
    touching depends on no conversion being dropped."""
    conn = _conn(db)
    bystander = _new_product(conn, 'twins641 bystander bill')
    _sale(conn, 'IV6412-1', bystander, CODE)
    dup = _twin(conn, 'twins641 merges anyway')
    conn.close()

    code, report = _run(db)

    assert code == 0, report
    conn = _conn(db)
    try:
        assert _units(conn, dup) == {WORD: GROSS}
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IV6412-1'"
                            ).fetchone()[0] == CODE
    finally:
        conn.close()


def test_it_refuses_when_the_unit_map_lacks_the_translation(db):
    conn = _conn(db)
    pid = _twin(conn, 'twins641 no map row')
    conn.execute("DELETE FROM unit_map WHERE book='BSN5657' AND spelling=?", (CODE,))
    conn.commit()
    conn.close()

    code, report = _run(db)

    assert code == 2, report
    assert any('unit_map' in p for p in report['problems']), report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS, WORD: GROSS}
    finally:
        conn.close()


@pytest.mark.parametrize('where', ['unit_type', 'mapping', 'tier'])
def test_a_spelling_elsewhere_does_not_block(db, where):
    """Only bill lines feed `_sync_bsn_to_stock`. A `กร` in a unit_type, a mapping
    or a tier label is 193's and 196's business."""
    conn = _conn(db)
    pid = _twin(conn, f'twins641 spelling {where}')
    if where == 'unit_type':
        _new_product(conn, 'twins641 spelling holder', unit_type=CODE)
    elif where == 'mapping':
        conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)"
                     " VALUES ('TWINS641','x',?,?)", (pid, CODE))
    else:
        conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price)"
                     " VALUES (?,?,?)", (pid, f'1 {CODE}', 960))
    conn.commit()
    conn.close()

    assert _run(db)[0] == 0

    conn = _conn(db)
    try:
        assert _units(conn, pid) == {WORD: GROSS}
    finally:
        conn.close()


def test_an_invariant_failure_rolls_the_whole_thing_back(db):
    """The invariants are the last line of defence, so prove they can fire and
    that firing leaves nothing behind."""
    conn = _conn(db)
    pid = _twin(conn, 'twins641 invariant')
    conn.close()

    real_merge = merge_twins.merge

    def sabotage(c, dups):
        real_merge(c, dups)
        # take the twin too — the one row that must always survive
        c.execute("DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit=?", (pid, WORD))

    merge_twins.merge = sabotage
    try:
        code, report = _run(db)
    finally:
        merge_twins.merge = real_merge

    assert code == 2, report
    assert any('InvariantFailed' in p for p in report['problems']), report
    conn = _conn(db)
    try:
        assert _units(conn, pid) == {CODE: GROSS, WORD: GROSS}, 'rollback left something behind'
    finally:
        conn.close()


def test_the_script_cannot_change_cost_even_if_it_tried(db):
    """Found while writing the test above, and worth keeping: the #590 guard means
    a plain `sqlite3.connect` cannot write `products.cost_price` at all. That is
    why this script needs no `database.script_connection` — not because it
    promises to stay away from cost, but because it could not reach it."""
    conn = _conn(db)
    pid = _twin(conn, 'twins641 cost guard')
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute("UPDATE products SET cost_price = cost_price + 1 WHERE id = ?", (pid,))
        assert 'sendy_actor' in str(exc.value), str(exc.value)
    finally:
        conn.rollback()
        conn.close()


# ── the #609 rule: what the importer produces afterwards ────────────────────

def test_a_raw_gross_line_still_resolves_and_moves_no_stock(db):
    """An Express line whose raw unit is the short code must still land on the
    word and still convert at 144 after the merge."""
    import config
    import models

    conn = _conn(db)
    pid = _twin(conn, 'twins641 reimport')
    conn.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)"
                 " VALUES ('TWINS641R','x',?,'')", (pid,))
    _sale(conn, 'IV6413-1', pid, WORD, qty=1, price=960, code='TWINS641R', synced=1)
    conn.commit()
    row = conn.execute("SELECT * FROM sales_transactions WHERE doc_no='IV6413-1'").fetchone()
    before_stock = conn.execute(
        "SELECT COALESCE((SELECT quantity FROM stock_levels WHERE product_id=?),0)",
        (pid,)).fetchone()[0]
    conn.close()

    assert _run(db)[0] == 0
    assert config.DATABASE_PATH == db, 'tmp_db must have redirected the app at the clone'

    entry = _entry(row, 'sales')
    entry['unit'] = CODE                     # what Express actually ships
    res = models.import_weekly([entry], 'sales', 'twins641-reimport', apply_removals=False)

    conn = _conn(db)
    try:
        assert res['unchanged'] == 1, res
        assert res['overwritten'] == 0 and res['imported'] == 0, res
        assert conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IV6413-1'"
                            ).fetchone()[0] == WORD
        assert conn.execute(
            "SELECT COALESCE((SELECT quantity FROM stock_levels WHERE product_id=?),0)",
            (pid,)).fetchone()[0] == before_stock
    finally:
        conn.close()


def test_it_goes_red_when_the_map_forgets_the_code(db):
    """CONTROL for the test above: without the map row the code stops resolving to
    the word, so that assertion is not vacuously true."""
    import bsn_units

    conn = _conn(db)
    _twin(conn, 'twins641 control')
    conn.close()

    assert _run(db)[0] == 0

    conn = _conn(db)
    try:
        assert bsn_units.normalize_unit(CODE, 'BSN5657', conn=conn) == WORD
        conn.execute("DELETE FROM unit_map WHERE book='BSN5657' AND spelling=?", (CODE,))
        conn.commit()
        assert bsn_units.normalize_unit(CODE, 'BSN5657', conn=conn) != WORD
    finally:
        conn.rollback()
        conn.close()
