"""mig 198: sales_line_unit_corrections (#692). The table CHECKs are the state
machine, so they are tested as the contract they are."""
import json
import os
import sqlite3

import pytest

_MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'data', 'migrations')
_UP = os.path.join(_MIGRATIONS, '198_sales_line_unit_corrections.sql')
_DOWN = os.path.join(_MIGRATIONS, '198_sales_line_unit_corrections.rollback.sql')

_ACTIVE = {
    'doc_no': 'IV6900001-1', 'bsn_code': 'C1', 'doc_base': 'IV6900001',
    'product_id': 1, 'express_unit_raw': 'โหล', 'express_unit': 'โหล', 'qty': 2,
    'unit_price': 49, 'net': 98, 'corrected_unit': 'หลอด', 'stock_mode': 'hold',
    'reason': 'คีย์หน่วยผิดจากออเดอร์จริง', 'created_by': 'put',
}
_ENDED = {'ended_at': '2026-09-30 10:00:00', 'ended_by': 'put'}


def _script(conn, path):
    with open(path, encoding='utf-8') as f:
        conn.executescript(f.read())


def _has_table(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sales_line_unit_corrections'"
    ).fetchone()[0] == 1


def _insert(conn, **overrides):
    row = dict(_ACTIVE, **overrides)
    cols = ', '.join(row)
    return conn.execute(
        f"INSERT INTO sales_line_unit_corrections ({cols})"
        f" VALUES ({', '.join('?' * len(row))})", list(row.values())).lastrowid


@pytest.fixture
def migrated(empty_db):
    """empty_db clones the live schema, so rebuild the pre-state first."""
    conn = sqlite3.connect(empty_db)
    _script(conn, _DOWN)
    assert not _has_table(conn)
    _script(conn, _UP)
    yield conn
    conn.close()


def test_rollback_drops_the_table_and_the_migration_rebuilds_it(migrated):
    assert _has_table(migrated)
    _script(migrated, _DOWN)
    assert not _has_table(migrated)
    _script(migrated, _UP)
    assert _has_table(migrated)


def test_a_second_apply_keeps_the_rows(migrated):
    _insert(migrated)
    migrated.commit()

    _script(migrated, _UP)

    assert migrated.execute(
        "SELECT COUNT(*) FROM sales_line_unit_corrections").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated)


def test_one_active_correction_per_line_and_any_number_of_ended_ones(migrated):
    _insert(migrated, status='cancelled', end_cause='cancelled', **_ENDED)
    _insert(migrated, status='retired', end_cause='express_changed', **_ENDED)
    _insert(migrated)
    _insert(migrated, doc_no='IV6900001-2')
    _insert(migrated, bsn_code='C2')

    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated)


@pytest.mark.parametrize('ok', [
    {},
    dict(status='cancelled', end_cause='cancelled', **_ENDED),
    dict(status='retired', end_cause='express_changed', **_ENDED),
    dict(status='retired', end_cause='express_removed', **_ENDED),
    dict(status='retired', end_cause='express_agrees', **_ENDED),
    dict(stock_mode='move', offset_txn_id=None),
])
def test_each_state_has_a_shape_the_table_accepts(migrated, ok):
    assert _insert(migrated, **ok) > 0


@pytest.mark.parametrize('bad', [
    dict(status='active', **_ENDED),
    dict(status='active', end_cause='cancelled'),
    dict(status='cancelled', end_cause='cancelled'),
    dict(status='cancelled', **_ENDED),
    dict(status='cancelled', end_cause='express_changed', **_ENDED),
    dict(status='retired', end_cause='cancelled', **_ENDED),
    dict(status='retired', end_cause='something_else', **_ENDED),
    dict(status='retired', **_ENDED),
    dict(status='reopened'),
    dict(stock_mode='guess'),
    dict(reason='สั้นไป'),
    dict(reason='              '),
], ids=lambda b: '-'.join(f'{k}={v}' for k, v in b.items() if k not in _ENDED)[:60])
def test_a_shape_outside_the_state_machine_is_refused(migrated, bad):
    assert _insert(migrated, doc_no='control') > 0

    with pytest.raises(sqlite3.IntegrityError):
        _insert(migrated, **bad)


def _audit(conn, row_id):
    return [(action, json.loads(fields), user, row_key) for action, fields, user, row_key
            in conn.execute(
                "SELECT action, changed_fields, user, row_key FROM audit_log"
                " WHERE table_name='sales_line_unit_corrections' AND row_id=?"
                " ORDER BY id", (row_id,))]


def test_every_write_to_a_correction_is_audited(migrated):
    rid = _insert(migrated)
    migrated.execute(
        "UPDATE sales_line_unit_corrections SET status='cancelled',"
        " end_cause='cancelled', ended_at='2026-09-30 10:00:00', ended_by='mam',"
        " end_reason='แก้ผิดบรรทัด ยกเลิก' WHERE id=?", (rid,))
    migrated.execute("UPDATE sales_line_unit_corrections SET created_at=created_at"
                     " WHERE id=?", (rid,))
    migrated.execute("DELETE FROM sales_line_unit_corrections WHERE id=?", (rid,))

    inserted, updated, deleted = _audit(migrated, rid)

    assert inserted[0] == 'INSERT' and inserted[2:] == ('put', 'IV6900001-1|C1')
    assert (inserted[1]['corrected_unit'], inserted[1]['express_unit_raw'],
            inserted[1]['stock_mode'], inserted[1]['reason']) == \
        ('หลอด', 'โหล', 'hold', 'คีย์หน่วยผิดจากออเดอร์จริง')
    assert updated == ('UPDATE', {
        'status': ['active', 'cancelled'], 'end_cause': [None, 'cancelled'],
        'ended_at': [None, '2026-09-30 10:00:00'], 'ended_by': [None, 'mam'],
        'end_reason': [None, 'แก้ผิดบรรทัด ยกเลิก']}, 'mam', 'IV6900001-1|C1')
    assert deleted[0] == 'DELETE' and deleted[1]['status'] == 'cancelled'


def test_rollback_drops_the_audit_triggers(migrated):
    def triggers():
        return migrated.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            " AND name LIKE 'audit_sales_line_unit_corrections_%'").fetchone()[0]
    assert triggers() == 3
    _script(migrated, _DOWN)
    assert triggers() == 0
