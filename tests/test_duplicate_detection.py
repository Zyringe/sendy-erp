"""
Duplicate detection in BSN weekly import.

See models.import_weekly (models.py:730+). For BSN-format doc_no
(contains '-', e.g. 'IV6900503-1') the duplicate check is:

    SELECT ... WHERE bsn_code = ? AND (doc_no = ? OR doc_no = doc_base)

→ Same (doc_no, bsn_code) is a duplicate.
→ Same doc_no with a different bsn_code is NOT a duplicate (different line item).

Tests drive import_weekly directly to keep the surface area small.
"""
import os
import sqlite3

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")


def _migrate124(db_path):
    """empty_db clones the live schema, which doesn't have mig 124 (bsn_unit
    restore) applied on this machine yet — apply it so import_weekly's
    _resolve_mapping call doesn't hit 'no such column: bsn_unit'."""
    conn = sqlite3.connect(db_path)
    with open(_MIG_124, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.close()


def _entry(*, doc_no, bsn_code, qty=1.0, unit='ตัว', unit_price=10.0,
           vat_type=1, total=10.0, net=10.0, name='สินค้า', party='ลูกค้า',
           party_code='C001', date_iso='2026-04-01'):
    return {
        'date_iso':         date_iso,
        'doc_no':           doc_no,
        'qty':              qty,
        'unit':             unit,
        'unit_price':       unit_price,
        'vat_type':         vat_type,
        'discount':         '',
        'total':            total,
        'net':              net,
        'product_name_raw': name,
        'product_code_raw': bsn_code,
        'party':            party,
        'party_code':       party_code,
    }


def test_same_doc_no_different_bsn_code_is_not_duplicate(empty_db, monkeypatch):
    """Two line items on the same invoice (different products) must both import."""
    import models

    _migrate124(empty_db)
    e1 = _entry(doc_no='IV6900999-1', bsn_code='AAA001', name='Item A')
    e2 = _entry(doc_no='IV6900999-2', bsn_code='BBB002', name='Item B')

    stats = models.import_weekly([e1, e2], 'sales', 'sample.csv')

    import sqlite3
    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_no, bsn_code FROM sales_transactions WHERE doc_base='IV6900999'"
        " ORDER BY id"
    ).fetchall()
    conn.close()

    assert len(rows) == 2, f"both line items should import, got {len(rows)}"
    assert {r['bsn_code'] for r in rows} == {'AAA001', 'BBB002'}


def test_same_doc_no_and_bsn_code_is_duplicate(empty_db):
    """
    Re-importing the same (doc_no, bsn_code) overwrites the existing row.
    The end state is exactly one row, not two.
    """
    import models

    _migrate124(empty_db)
    e = _entry(doc_no='IV6901000-1', bsn_code='CCC003', name='Item C',
               unit_price=10.0, net=10.0)
    models.import_weekly([e], 'sales', 'first.csv')

    # Re-import: same doc_no + same bsn_code → existing row should be replaced.
    e2 = _entry(doc_no='IV6901000-1', bsn_code='CCC003', name='Item C (updated)',
                unit_price=10.0, net=12.0)
    models.import_weekly([e2], 'sales', 'second.csv')

    import sqlite3
    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_no, bsn_code, net, product_name_raw"
        " FROM sales_transactions WHERE doc_no='IV6901000-1' AND bsn_code='CCC003'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected exactly 1 row after re-import, got {len(rows)}"
    # And it should be the latest version.
    assert rows[0]['net'] == 12.0
    assert rows[0]['product_name_raw'] == 'Item C (updated)'


def test_same_doc_base_same_bsn_different_unit_price_keeps_both(empty_db):
    """
    Two BSN line items can share doc_base + bsn_code but differ in unit_price
    (e.g. partial returns priced differently). Both should import.
    """
    import models

    _migrate124(empty_db)
    e1 = _entry(doc_no='IV6901001-1', bsn_code='DDD004', unit_price=10.0, net=10.0)
    e2 = _entry(doc_no='IV6901001-2', bsn_code='DDD004', unit_price=15.0, net=15.0)
    models.import_weekly([e1, e2], 'sales', 'sample.csv')

    import sqlite3
    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_no, unit_price FROM sales_transactions WHERE doc_base='IV6901001'"
        " ORDER BY doc_no"
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert [r['unit_price'] for r in rows] == [10.0, 15.0]
