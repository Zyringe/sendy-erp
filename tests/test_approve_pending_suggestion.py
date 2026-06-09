"""Regression tests for approve_pending_suggestion (models.py).

Covers:
- units_per_carton/box = NULL must NOT block approval (mig 069 made them
  NOT NULL DEFAULT 1; backend defaults to 1 when frontend sends null).
- Mapping row uses sug.bsn_unit as scope (strict mode), not hardcoded ''.
"""
import sqlite3

import pytest


@pytest.fixture
def empty_db_with_user(empty_db):
    """empty_db + a single user row so pending_product_suggestions's FK to
    users(id) is satisfied. Returns (db_path, user_id)."""
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active) "
        "VALUES ('tester', 'x', 'Tester', 'admin', 1)"
    )
    uid = conn.execute("SELECT id FROM users WHERE username='tester'").fetchone()[0]
    conn.commit()
    conn.close()
    return empty_db, uid


def _stage_minimal(payload, *, user_id=1):
    """Insert a pending suggestion with the given payload (already merged
    with defaults). Returns the new suggestion id."""
    import models
    return models.save_pending_suggestion(payload, user_id=user_id)


def _stage_payload(bsn_code, *, bsn_unit=None, units_per_carton=None,
                   units_per_box=None):
    return {
        'bsn_code': bsn_code,
        'bsn_name': f'test {bsn_code}',
        'suggested_name': f'product for {bsn_code}',
        'category': None, 'series': None, 'brand_id': None,
        'model': None, 'size': None, 'color_th': None, 'color_code': None,
        'packaging': None, 'condition': None, 'pack_variant': None,
        'suggested_cost': 10.0,
        'suggested_unit_type': 'ตัว',
        'units_per_carton': units_per_carton,
        'units_per_box': units_per_box,
        'bsn_unit': bsn_unit,
        'unit_conversion_ratio': None,
    }


def test_approve_with_null_units_defaults_to_one(empty_db_with_user):
    """User leaves Units/carton and Units/box blank → mig 069 NOT NULL must
    not break approval. Backend defaults to 1 (the schema's DEFAULT value)."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload('TEST001'), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT units_per_carton, units_per_box FROM products WHERE id = ?",
        (new_pid,),
    ).fetchone()
    conn.close()

    assert row['units_per_carton'] == 1
    assert row['units_per_box'] == 1


def test_approve_preserves_explicit_units(empty_db_with_user):
    """When the user did fill the fields, those values must be preserved."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload(
        'TEST002', units_per_carton=12, units_per_box=6,
    ), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT units_per_carton, units_per_box FROM products WHERE id = ?",
        (new_pid,),
    ).fetchone()
    conn.close()

    assert row['units_per_carton'] == 12
    assert row['units_per_box'] == 6


def test_approve_scopes_mapping_by_bsn_unit(empty_db_with_user):
    """Strict mode: the mapping row's bsn_unit must equal the suggestion's
    bsn_unit (not hardcoded '')."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload('TEST003', bsn_unit='แผง'), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT bsn_unit, product_id FROM product_code_mapping "
        "WHERE bsn_code = ?",
        ('TEST003',),
    ).fetchone()
    conn.close()

    assert row['bsn_unit'] == 'แผง'
    assert row['product_id'] == new_pid


def test_approve_clears_pending_placeholder(empty_db_with_user):
    """Regression: import flow created a catch-all placeholder
    (bsn_unit='', product_id=NULL). When approve creates a SCOPED mapping
    (bsn_unit='แผง'), the placeholder must be deleted so the bsn_code
    disappears from get_pending_mappings() — otherwise the user still sees
    the code in 'ผูกรหัส BSN' even after admin approval."""
    empty_db, uid = empty_db_with_user
    import models

    # Simulate import-time placeholder
    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)
        VALUES ('TEST005', 'test 005', NULL, '')
    """)
    conn.commit()
    conn.close()

    # Pre-check: shows in pending
    assert any(r['bsn_code'] == 'TEST005' for r in models.get_pending_mappings())

    sid = _stage_minimal(_stage_payload('TEST005', bsn_unit='แผง'), user_id=uid)
    models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    # Post-check: NOT in pending anymore
    assert not any(r['bsn_code'] == 'TEST005' for r in models.get_pending_mappings())


def test_upsert_mapping_preserves_ignored_marker(empty_db_with_user):
    """When user previously chose '(C) ข้าม' (is_ignored=1, with reason),
    later mapping the same code to a scoped SKU must NOT wipe the ignored
    marker — its ignore_reason is the audit trail of the original decision.
    Regression for an over-broad placeholder-cleanup DELETE."""
    empty_db, uid = empty_db_with_user
    import models

    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping
            (bsn_code, bsn_name, product_id, bsn_unit, is_ignored, ignore_reason)
        VALUES ('TEST007', 'test 007', NULL, '', 1, 'ค่าขนส่ง')
    """)
    cur = conn.execute("INSERT INTO products (product_name, unit_type) VALUES ('existing', 'ตัว')")
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.commit()
    conn.close()

    models.upsert_mapping('TEST007', 'test 007', product_id=pid, bsn_unit='แผง')

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT bsn_unit, product_id, is_ignored, ignore_reason "
        "FROM product_code_mapping WHERE bsn_code='TEST007' "
        "ORDER BY bsn_unit"
    ).fetchall()
    conn.close()

    # Expect BOTH rows present: the ignored catch-all and the new scoped row
    assert len(rows) == 2
    catch_all = rows[0]  # bsn_unit='' sorts first
    scoped    = rows[1]  # bsn_unit='แผง'
    assert catch_all['bsn_unit'] == ''
    assert catch_all['is_ignored'] == 1
    assert catch_all['ignore_reason'] == 'ค่าขนส่ง'
    assert scoped['bsn_unit'] == 'แผง'
    assert scoped['product_id'] == pid


def test_upsert_mapping_clears_pending_placeholder(empty_db_with_user):
    """Same regression for the direct Card-A map flow (upsert_mapping)."""
    empty_db, uid = empty_db_with_user
    import models

    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)
        VALUES ('TEST006', 'test 006', NULL, '')
    """)
    # Need an existing product to map to
    cur = conn.execute("INSERT INTO products (product_name, unit_type) VALUES ('existing', 'ตัว')")
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.commit()
    conn.close()

    models.upsert_mapping('TEST006', 'test 006', product_id=pid, bsn_unit='แผง')

    assert not any(r['bsn_code'] == 'TEST006' for r in models.get_pending_mappings())


def test_approve_falls_back_to_catchall_when_bsn_unit_missing(empty_db_with_user):
    """When the suggestion has no bsn_unit (no purchase/sale history), the
    mapping falls back to the catch-all ''. Documented gap: such suggestions
    should ideally be flagged before approval."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload('TEST004', bsn_unit=None), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT bsn_unit FROM product_code_mapping WHERE bsn_code = ?",
        ('TEST004',),
    ).fetchone()
    conn.close()

    assert row['bsn_unit'] == ''
