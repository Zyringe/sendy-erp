"""Migration 199 — promotions.min_qty + min_qty_unit (#673): an enforced
minimum quantity on a PRICE promo.

Every test forces its own rows on throwaway products (`tmp_db_conn` clones the
live dev DB WITH data — never inherit it). Drop-first fixture: the clone may
already carry 199 (init_db applies any migration file present), so the `db`
fixture runs the ROLLBACK first, then this test's own forward run.

⚠ The rollback-trigger-byte-identity test compares against
EXPECTED_176_TRIGGER_SQL, a LITERAL extracted from `git show
bc4183f:data/schema.sql` (origin/main before 199 existed) and checked equal to
sqlite_master on the seeded worktree DB — never against a `before` snapshot the
fixture produced by running the same rollback file (that compares the file to
itself; see tests/test_mig176_promo_source.py).
"""
import json
import sqlite3
from pathlib import Path

import pytest

from models import promotions as promo_models

MIGS = Path(__file__).resolve().parents[1] / 'data/migrations'
MIG = MIGS / '199_promo_min_qty.sql'
ROLLBACK = MIGS / '199_promo_min_qty.rollback.sql'
AUDIT_TRIGGERS = ('audit_promotions_insert', 'audit_promotions_update', 'audit_promotions_delete')

EXPECTED_176_TRIGGER_SQL = {
    'audit_promotions_insert': "CREATE TRIGGER audit_promotions_insert\nAFTER INSERT ON promotions\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    VALUES (\n        'promotions', NEW.id, 'INSERT',\n        json_object(\n            'product_id',        NEW.product_id,\n            'promo_name',        NEW.promo_name,\n            'promo_type',        NEW.promo_type,\n            'discount_value',    NEW.discount_value,\n            'bundle_buy',        NEW.bundle_buy,\n            'bundle_free',       NEW.bundle_free,\n            'bundle_unit',       NEW.bundle_unit,\n            'bundle_condition',  NEW.bundle_condition,\n            'bundle_tiers_json', NEW.bundle_tiers_json,\n            'gift_desc',         NEW.gift_desc,\n            'gift_qty',          NEW.gift_qty,\n            'source',            NEW.source,\n            'date_start',        NEW.date_start,\n            'date_end',          NEW.date_end,\n            'is_active',         NEW.is_active\n        )\n    );\nEND",
    'audit_promotions_update': "CREATE TRIGGER audit_promotions_update\nAFTER UPDATE ON promotions\nWHEN (\n       OLD.product_id        IS NOT NEW.product_id\n    OR OLD.promo_name        IS NOT NEW.promo_name\n    OR OLD.promo_type        IS NOT NEW.promo_type\n    OR OLD.discount_value    IS NOT NEW.discount_value\n    OR OLD.bundle_buy        IS NOT NEW.bundle_buy\n    OR OLD.bundle_free       IS NOT NEW.bundle_free\n    OR OLD.bundle_unit       IS NOT NEW.bundle_unit\n    OR OLD.bundle_condition  IS NOT NEW.bundle_condition\n    OR OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json\n    OR OLD.gift_desc         IS NOT NEW.gift_desc\n    OR OLD.gift_qty          IS NOT NEW.gift_qty\n    OR OLD.source            IS NOT NEW.source\n    OR OLD.date_start        IS NOT NEW.date_start\n    OR OLD.date_end          IS NOT NEW.date_end\n    OR OLD.is_active         IS NOT NEW.is_active\n)\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    SELECT 'promotions', NEW.id, 'UPDATE',\n           json_group_object(field, json_array(old_v, new_v))\n    FROM (\n                  SELECT 'product_id'        AS field, OLD.product_id        AS old_v, NEW.product_id        AS new_v WHERE OLD.product_id        IS NOT NEW.product_id\n        UNION ALL SELECT 'promo_name',                OLD.promo_name,                NEW.promo_name                WHERE OLD.promo_name        IS NOT NEW.promo_name\n        UNION ALL SELECT 'promo_type',                OLD.promo_type,                NEW.promo_type                WHERE OLD.promo_type        IS NOT NEW.promo_type\n        UNION ALL SELECT 'discount_value',            OLD.discount_value,            NEW.discount_value            WHERE OLD.discount_value    IS NOT NEW.discount_value\n        UNION ALL SELECT 'bundle_buy',                OLD.bundle_buy,                NEW.bundle_buy                WHERE OLD.bundle_buy        IS NOT NEW.bundle_buy\n        UNION ALL SELECT 'bundle_free',               OLD.bundle_free,               NEW.bundle_free               WHERE OLD.bundle_free       IS NOT NEW.bundle_free\n        UNION ALL SELECT 'bundle_unit',               OLD.bundle_unit,               NEW.bundle_unit               WHERE OLD.bundle_unit       IS NOT NEW.bundle_unit\n        UNION ALL SELECT 'bundle_condition',          OLD.bundle_condition,          NEW.bundle_condition          WHERE OLD.bundle_condition  IS NOT NEW.bundle_condition\n        UNION ALL SELECT 'bundle_tiers_json',         OLD.bundle_tiers_json,         NEW.bundle_tiers_json         WHERE OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json\n        UNION ALL SELECT 'gift_desc',                 OLD.gift_desc,                 NEW.gift_desc                 WHERE OLD.gift_desc         IS NOT NEW.gift_desc\n        UNION ALL SELECT 'gift_qty',                  OLD.gift_qty,                  NEW.gift_qty                  WHERE OLD.gift_qty          IS NOT NEW.gift_qty\n        UNION ALL SELECT 'source',                    OLD.source,                    NEW.source                    WHERE OLD.source            IS NOT NEW.source\n        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start\n        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end\n        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active\n    );\nEND",
    'audit_promotions_delete': "CREATE TRIGGER audit_promotions_delete\nBEFORE DELETE ON promotions\nBEGIN\n    INSERT INTO audit_log (table_name, row_id, action, changed_fields)\n    VALUES (\n        'promotions', OLD.id, 'DELETE',\n        json_object(\n            'product_id',        OLD.product_id,\n            'promo_name',        OLD.promo_name,\n            'promo_type',        OLD.promo_type,\n            'discount_value',    OLD.discount_value,\n            'bundle_buy',        OLD.bundle_buy,\n            'bundle_free',       OLD.bundle_free,\n            'bundle_unit',       OLD.bundle_unit,\n            'bundle_condition',  OLD.bundle_condition,\n            'bundle_tiers_json', OLD.bundle_tiers_json,\n            'gift_desc',         OLD.gift_desc,\n            'gift_qty',          OLD.gift_qty,\n            'source',            OLD.source,\n            'is_active',         OLD.is_active\n        )\n    );\nEND",
}


def _cols(conn):
    return {r['name'] for r in conn.execute("PRAGMA table_info(promotions)")}


@pytest.fixture
def db(tmp_db_conn):
    if 'min_qty' in _cols(tmp_db_conn):
        tmp_db_conn.executescript(ROLLBACK.read_text(encoding='utf-8'))
        tmp_db_conn.commit()
    assert 'min_qty' not in _cols(tmp_db_conn)
    tmp_db_conn.executescript(MIG.read_text(encoding='utf-8'))
    tmp_db_conn.commit()
    return tmp_db_conn


_pid = [970000]


def _mk_product(conn):
    _pid[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, is_active) "
        "VALUES (?, 'อัน', 100, 60, 1)", (f'mig199 #{_pid[0]}',))
    conn.commit()
    return cur.lastrowid


def _insert(conn, **cols):
    row = {'product_id': _mk_product(conn), 'promo_name': 'mig199', 'is_active': 1, **cols}
    cur = conn.execute(
        f"INSERT INTO promotions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()))
    conn.commit()
    return cur.lastrowid


# ── CHECK ────────────────────────────────────────────────────────────────────

ACCEPTED = [
    dict(promo_type='percent', discount_value=5, min_qty=20, min_qty_unit='อัน'),
    dict(promo_type='percent', discount_value=25, min_qty=5.0, min_qty_unit='โหล'),
    dict(promo_type='fixed', discount_value=40, min_qty=2, min_qty_unit='โหล'),
    dict(promo_type='mixed', discount_value=10, min_qty=3, min_qty_unit='กล่อง'),
    dict(promo_type='percent', discount_value=5),                      # no minimum
    dict(promo_type='bundle', bundle_buy=10, bundle_free=1),           # no minimum
]

REFUSED = {
    'qty without unit': dict(promo_type='percent', discount_value=5, min_qty=20),
    'unit without qty': dict(promo_type='percent', discount_value=5, min_qty_unit='อัน'),
    'zero': dict(promo_type='percent', discount_value=5, min_qty=0, min_qty_unit='อัน'),
    'negative': dict(promo_type='percent', discount_value=5, min_qty=-1, min_qty_unit='อัน'),
    'blank unit': dict(promo_type='percent', discount_value=5, min_qty=20, min_qty_unit='  '),
    'text qty': dict(promo_type='percent', discount_value=5, min_qty='abc', min_qty_unit='อัน'),
    'bundle row': dict(promo_type='bundle', bundle_buy=10, bundle_free=1,
                       min_qty=20, min_qty_unit='อัน'),
    'gift row': dict(promo_type='gift', gift_desc='x', gift_qty='1',
                     min_qty=20, min_qty_unit='อัน'),
    'mixed qty-only': dict(promo_type='mixed', bundle_buy=10, bundle_free=1,
                           min_qty=20, min_qty_unit='อัน'),
    'mixed price+bundle': dict(promo_type='mixed', discount_value=10, bundle_buy=10,
                               bundle_free=1, min_qty=20, min_qty_unit='อัน'),
    'mixed price+gift': dict(promo_type='mixed', discount_value=10, gift_desc='x',
                             gift_qty='1', min_qty=20, min_qty_unit='อัน'),
}


def test_check_accepts_every_valid_shape(db):
    # CONTROL first: a CHECK that refuses everything would pass the refusal test.
    ids = [_insert(db, **c) for c in ACCEPTED]
    assert len(ids) == len(ACCEPTED) == 6
    stored = db.execute(
        f"SELECT min_qty, min_qty_unit FROM promotions WHERE id IN ({','.join('?' * len(ids))}) "
        "ORDER BY id", ids).fetchall()
    assert [tuple(r) for r in stored] == [
        (20.0, 'อัน'), (5.0, 'โหล'), (2.0, 'โหล'), (3.0, 'กล่อง'), (None, None), (None, None)]


@pytest.mark.parametrize('label', sorted(REFUSED))
def test_check_refuses(db, label):
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        _insert(db, **REFUSED[label])


def test_update_into_a_bad_shape_is_refused_too(db):
    pid = _insert(db, promo_type='percent', discount_value=5, min_qty=20, min_qty_unit='อัน')
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        db.execute("UPDATE promotions SET min_qty_unit = NULL WHERE id = ?", (pid,))
    db.rollback()


def test_check_text_embeds_promo_slot_sql_price_expr_verbatim(db):
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='promotions'").fetchone()[0]
    price_expr, qty_expr = promo_models.promo_slot_sql('')
    assert 'min_qty_unit' in table_sql   # control: the column landed
    assert price_expr in table_sql
    # control: the qty expression is NOT what the CHECK asks for
    assert qty_expr not in table_sql


def test_existing_rows_read_null(db):
    n_rows = db.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
    assert n_rows > 0
    assert db.execute(
        "SELECT COUNT(*) FROM promotions WHERE min_qty IS NOT NULL OR min_qty_unit IS NOT NULL"
    ).fetchone()[0] == 0


# ── audit triggers ───────────────────────────────────────────────────────────

def _audit(conn, promo_id, action):
    rows = conn.execute(
        "SELECT changed_fields FROM audit_log WHERE table_name='promotions' AND row_id=? "
        "AND action=? ORDER BY id", (promo_id, action)).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_audit_rows_carry_the_minimum(db):
    pid = _insert(db, promo_type='percent', discount_value=5, min_qty=20, min_qty_unit='อัน')
    ins = _audit(db, pid, 'INSERT')
    assert len(ins) == 1
    assert (ins[0]['min_qty'], ins[0]['min_qty_unit']) == (20.0, 'อัน')
    assert ins[0]['source'] is None     # control: 176's columns are still recorded

    db.execute("UPDATE promotions SET min_qty = 10 WHERE id = ?", (pid,))
    db.execute("UPDATE promotions SET min_qty_unit = 'ตัว' WHERE id = ?", (pid,))
    db.commit()
    upd = _audit(db, pid, 'UPDATE')
    assert len(upd) == 2, 'an UPDATE of either column alone must fire the trigger'
    assert upd[0] == {'min_qty': [20.0, 10.0]}
    assert upd[1] == {'min_qty_unit': ['อัน', 'ตัว']}

    db.execute("DELETE FROM promotions WHERE id = ?", (pid,))
    db.commit()
    dele = _audit(db, pid, 'DELETE')
    assert len(dele) == 1
    assert (dele[0]['min_qty'], dele[0]['min_qty_unit']) == (10.0, 'ตัว')


# ── rollback ─────────────────────────────────────────────────────────────────

def _trigger_bodies(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
        AUDIT_TRIGGERS)}


def test_rollback_restores_176_triggers_byte_identical_and_drops_columns(db):
    assert _trigger_bodies(db) != EXPECTED_176_TRIGGER_SQL   # control: forward changed them
    db.executescript(ROLLBACK.read_text(encoding='utf-8'))
    db.commit()
    assert _trigger_bodies(db) == EXPECTED_176_TRIGGER_SQL
    cols = _cols(db)
    assert 'source' in cols                                   # control: 176 untouched
    assert not ({'min_qty', 'min_qty_unit'} & cols)
    # re-runnable: forward again after a rollback
    db.executescript(MIG.read_text(encoding='utf-8'))
    db.commit()
    assert {'min_qty', 'min_qty_unit'} <= _cols(db)


def test_rollback_aborts_when_an_active_promo_carries_a_minimum(db):
    pid = _insert(db, promo_type='percent', discount_value=5, min_qty=20, min_qty_unit='อัน')
    with pytest.raises(sqlite3.DatabaseError, match='199 rollback precondition'):
        db.executescript(ROLLBACK.read_text(encoding='utf-8'))
    db.rollback()
    assert {'min_qty', 'min_qty_unit'} <= _cols(db)
    assert db.execute("SELECT min_qty FROM promotions WHERE id = ?", (pid,)).fetchone()[0] == 20.0

    # an INACTIVE row with a minimum does not block it (dropping it ungates nothing live)
    db.execute("UPDATE promotions SET is_active = 0 WHERE id = ?", (pid,))
    db.commit()
    db.executescript(ROLLBACK.read_text(encoding='utf-8'))
    db.commit()
    assert 'min_qty' not in _cols(db)
