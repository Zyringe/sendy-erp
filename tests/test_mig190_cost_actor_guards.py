"""#590 PR 2: migration 190 — every cost write carries who made it.

Design §A3 (docs/specs/2026-09-19-590-cost-audit-actor-design.md, branch
feat/590-cost-audit-actor): the guard table, verb by verb. Each test pairs the
refusal with a CONTROL the same statement passes once an actor is declared, so
a red here cannot be a broken fixture.

Built from `data/schema.sql` (the fresh-DB baseline), not the live DB: another
tab's dev DB must not decide whether this migration's triggers exist.
"""
import json
import os
import re
import sqlite3

import pytest

import actor
import database

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG = os.path.join(REPO, 'data', 'migrations', '190_cost_actor_guards.sql')
ROLLBACK = os.path.join(REPO, 'data', 'migrations', '190_cost_actor_guards.rollback.sql')
REFUSAL = 'ต้องระบุตัวผู้แก้ต้นทุน'
PUT = actor.Actor(source='manual', who='put', kind='script', detail='x.py: why')


@pytest.fixture
def fresh(tmp_path):
    """A DB built from schema.sql with one product, one ledger row and one
    conversion-log row, written on a connection the guards cannot see (the
    rows exist before the test starts, the way prod's do)."""
    path = str(tmp_path / 'fresh.db')
    raw = sqlite3.connect(path)
    with open(os.path.join(REPO, 'data', 'schema.sql'), encoding='utf-8') as f:
        raw.executescript(f.read())
    raw.close()
    seed = actor.install(sqlite3.connect(path), PUT)
    seed.execute("INSERT INTO products (id, product_name, cost_price, opening_cost)"
                 " VALUES (1, 'สินค้าทดสอบ', 10, 10)")
    seed.execute("INSERT INTO product_cost_ledger (product_id, event_type, event_date,"
                 " qty_change, unit_cost, stock_after, wacc_after)"
                 " VALUES (1, 'INITIAL', '2026-03-03', 5, 10, 5, 10)")
    seed.execute("INSERT INTO conversion_cost_log (output_product_id, event_date,"
                 " output_qty, total_input_cost, unit_cost) VALUES (1, '2026-09-01', 1, 10, 10)")
    seed.commit()
    seed.close()
    actor.set_fallback(None)          # 'nobody declared' must be observable
    return path


def _unsigned(path):
    return actor.install(sqlite3.connect(path), None)


def _signed(path, who=PUT):
    return actor.install(sqlite3.connect(path), who)


def _refused(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError, match=REFUSAL):
        conn.execute(sql, params)
    conn.rollback()


# ── products: UPDATE OF cost_price / opening_cost ────────────────────────────

@pytest.mark.parametrize('column', ['cost_price', 'opening_cost'])
def test_an_unsigned_cost_change_is_refused_and_a_signed_one_lands(fresh, column):
    _refused(_unsigned(fresh), f'UPDATE products SET {column} = 11 WHERE id = 1')
    c = _signed(fresh)
    c.execute(f'UPDATE products SET {column} = 11 WHERE id = 1')
    assert c.execute(f'SELECT {column} FROM products WHERE id = 1').fetchone()[0] == 11


def test_setting_the_same_cost_is_not_a_change(fresh):
    c = _unsigned(fresh)
    c.execute('UPDATE products SET cost_price = 10 WHERE id = 1')      # no refusal
    assert c.execute("SELECT count(*) FROM audit_log WHERE table_name = 'products'"
                     " AND row_id = 1 AND action = 'UPDATE'").fetchone()[0] == 0


def test_a_raw_connection_still_renames_but_cannot_touch_cost(fresh):
    raw = sqlite3.connect(fresh)
    raw.execute("UPDATE products SET product_name = 'ชื่อใหม่' WHERE id = 1")   # control
    with pytest.raises(sqlite3.OperationalError, match='no such function: sendy_actor'):
        raw.execute('UPDATE products SET cost_price = 12 WHERE id = 1')


def test_a_signed_cost_change_is_audited_with_its_actor(fresh):
    c = _signed(fresh)
    c.execute('UPDATE products SET cost_price = 12, opening_cost = 12 WHERE id = 1')
    rows = c.execute("SELECT changed_fields, user, change_source, change_reason FROM audit_log"
                     " WHERE table_name = 'products' AND row_id = 1 AND action = 'UPDATE'").fetchall()
    assert len(rows) == 1
    fields, user, source, reason = rows[0]
    assert json.loads(fields) == {'cost_price': [10.0, 12.0], 'opening_cost': [10.0, 12.0]}
    assert (user, source, reason) == ('put', 'manual', 'script:x.py: why')


def test_a_mixed_update_splits_into_an_unsigned_row_and_a_signed_cost_row(fresh):
    c = _signed(fresh)
    c.execute('UPDATE products SET base_sell_price = 99, cost_price = 13 WHERE id = 1')
    rows = c.execute("SELECT changed_fields, user FROM audit_log WHERE table_name = 'products'"
                     " AND row_id = 1 AND action = 'UPDATE' ORDER BY id").fetchall()
    assert len(rows) == 2
    general = [json.loads(f) for f, u in rows if u is None]
    costed = [json.loads(f) for f, u in rows if u == 'put']
    assert general == [{'base_sell_price': [0.0, 99.0]}]
    assert costed == [{'cost_price': [10.0, 13.0]}]


# ── product_cost_ledger: every verb ──────────────────────────────────────────

LEDGER = {
    'INSERT': ("INSERT INTO product_cost_ledger (product_id, event_type, event_date,"
               " qty_change, unit_cost, stock_after, wacc_after)"
               " VALUES (1, 'PURCHASE', '2026-09-01', 1, 10, 6, 10)"),
    'UPDATE': 'UPDATE product_cost_ledger SET unit_cost = 9 WHERE product_id = 1',
    'DELETE': 'DELETE FROM product_cost_ledger WHERE product_id = 1',
}


@pytest.mark.parametrize('verb', sorted(LEDGER))
def test_the_ledger_refuses_every_unsigned_verb(fresh, verb):
    _refused(_unsigned(fresh), LEDGER[verb])
    c = _signed(fresh)
    c.execute(LEDGER[verb])                                          # control
    c.commit()


# ── conversion_cost_log: every verb, and a stamp nobody can supply ───────────

CONV = {
    'INSERT': ("INSERT INTO conversion_cost_log (output_product_id, event_date, output_qty,"
               " total_input_cost, unit_cost) VALUES (1, '2026-09-02', 1, 20, 20)"),
    'UPDATE': 'UPDATE conversion_cost_log SET unit_cost = 9 WHERE output_product_id = 1',
    'DELETE': 'DELETE FROM conversion_cost_log WHERE output_product_id = 1',
}


@pytest.mark.parametrize('verb', sorted(CONV))
def test_the_conversion_log_refuses_every_unsigned_verb(fresh, verb):
    """⚠ An unsigned INSERT is refused TWICE over: by the insert guard, and, if
    that were gone, by the update guard the stamp trigger fires. So deleting the
    insert guard alone leaves this green (break-it-once G3); the run deletes
    both (G3b) to prove the refusal is real."""
    _refused(_unsigned(fresh), CONV[verb])
    c = _signed(fresh)
    c.execute(CONV[verb])                                            # control
    c.commit()


def test_the_conversion_log_is_stamped_by_the_database(fresh):
    c = _signed(fresh)
    cur = c.execute(CONV['INSERT'])
    stamp = c.execute('SELECT written_by FROM conversion_cost_log WHERE id = ?',
                      (cur.lastrowid,)).fetchone()[0]
    assert json.loads(stamp) == {'who': 'put', 'source': 'manual', 'reason': 'script:x.py: why'}


def test_a_caller_cannot_supply_or_rewrite_the_stamp(fresh):
    c = _signed(fresh)
    # Two different guards, each named by its own message: the supply guard must
    # be what refuses this, not the rewrite guard catching it one step later.
    with pytest.raises(sqlite3.IntegrityError, match='do not supply it'):
        c.execute("INSERT INTO conversion_cost_log (output_product_id, event_date, output_qty,"
                  " total_input_cost, unit_cost, written_by) VALUES (1, '2026-09-03', 1, 1, 1, 'me')")
    c.rollback()
    cur = c.execute(CONV['INSERT'])
    with pytest.raises(sqlite3.IntegrityError, match='never rewritten'):
        c.execute("UPDATE conversion_cost_log SET written_by = 'me' WHERE id = ?", (cur.lastrowid,))


# ── the migration itself ─────────────────────────────────────────────────────

@pytest.fixture
def migrated(tmp_db):
    """The live-DB copy brought to this tree's level by the real boot path."""
    database.init_db()
    return tmp_db


def test_existing_conversion_rows_are_backfilled_as_legacy(migrated):
    conn = sqlite3.connect(migrated)
    total, legacy = conn.execute(
        "SELECT count(*), sum(written_by = 'legacy:pre-590') FROM conversion_cost_log").fetchone()
    assert total > 0, 'the live-DB copy holds conversion rows; an empty table proves nothing'
    assert legacy == total


def _objects(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    finally:
        conn.close()


def _pre_190_audit_trigger():
    """audit_products_update exactly as migration 159 last defined it."""
    with open(os.path.join(REPO, 'data', 'migrations', '159_product_parcel_weight.sql'),
              encoding='utf-8') as f:
        return re.search(r'CREATE TRIGGER audit_products_update.*?\nEND', f.read(), re.S).group(0)


def test_the_rollback_restores_the_pre_190_schema_byte_for_byte(migrated):
    tmp_db = migrated
    after = _objects(tmp_db)
    conn = actor.install(sqlite3.connect(tmp_db), PUT)
    with open(ROLLBACK, encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.close()
    rolled = _objects(tmp_db)
    trig = [sql for t, n, _, sql in rolled if n == 'audit_products_update']
    assert trig == [_pre_190_audit_trigger()]
    names = {n for _, n, _, _ in rolled}
    assert not names & {'products_cost_needs_actor', 'audit_products_cost_update'}
    assert 'written_by' not in dict(((n, s) for _, n, _, s in rolled))['conversion_cost_log']
    # forward again lands exactly where the first forward did
    conn = actor.install(sqlite3.connect(tmp_db), PUT)
    with open(MIG, encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.close()
    again = _objects(tmp_db)
    assert again == after


# ── the migration runner signs what it runs ─────────────────────────────────

def test_a_migration_that_moves_cost_is_signed_by_the_runner(migrated, tmp_path, monkeypatch):
    mig_dir = tmp_path / 'migrations'
    mig_dir.mkdir()
    (mig_dir / '999_test_cost_move.sql').write_text(
        'BEGIN;\nUPDATE products SET cost_price = cost_price + 1'
        ' WHERE id = (SELECT min(id) FROM products);\nCOMMIT;\n', encoding='utf-8')
    monkeypatch.setattr(database, 'MIGRATIONS_DIR', str(mig_dir))
    actor.set_fallback(None)
    conn = database.get_connection()
    try:
        before = conn.execute('SELECT max(id) FROM audit_log').fetchone()[0]
        database.run_pending_migrations(conn, verbose=False)
        row = [tuple(r) for r in conn.execute(
            "SELECT user, change_source, change_reason FROM audit_log"
            " WHERE id > ? AND table_name = 'products'", (before,))]
    finally:
        conn.close()
    assert row == [('deploy', 'migration', 'migration:999_test_cost_move.sql')]
