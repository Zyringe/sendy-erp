"""Regression tests for approve_pending_suggestion (models.py).

Covers:
- units_per_carton/box = NULL must NOT block approval (mig 069 made them
  NOT NULL DEFAULT 1; backend defaults to 1 when frontend sends null).
- Mapping row is scoped to the non-split catch-all (bsn_unit='') — mig 124
  restored the column but approve still writes the generic catch-all row,
  not a unit-specific split row (see models.py::approve_pending_suggestion).

⚠ 2026-07-02: `empty_db` clones the LIVE schema, which doesn't have mig 124
applied on this machine yet (implementer session never touches the live DB —
see erp-engineering-discipline.md). `empty_db_with_user` applies mig 124
itself so every test below (all going through this one fixture) sees the
restored bsn_unit column.
"""
import os
import sqlite3

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")


@pytest.fixture
def empty_db_with_user(empty_db):
    """empty_db + mig 124 (bsn_unit restore) + a single user row so
    pending_product_suggestions's FK to users(id) is satisfied. Returns
    (db_path, user_id)."""
    conn = sqlite3.connect(empty_db)
    with open(_MIG_124, encoding="utf-8") as f:
        conn.executescript(f.read())
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


def test_approve_clears_pending_placeholder(empty_db_with_user):
    """Regression: import creates a pending placeholder (product_id=NULL).
    After approve maps the code, it must disappear from get_pending_mappings()
    — otherwise the user still sees it in 'ผูกรหัส BSN' after approval."""
    empty_db, uid = empty_db_with_user
    import models

    # Simulate import-time placeholder
    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)
        VALUES ('TEST005', 'test 005', NULL)
    """)
    conn.commit()
    conn.close()

    # Pre-check: shows in pending
    assert any(r['bsn_code'] == 'TEST005' for r in models.get_pending_mappings())

    sid = _stage_minimal(_stage_payload('TEST005', bsn_unit='แผง'), user_id=uid)
    models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    # Post-check: NOT in pending anymore
    assert not any(r['bsn_code'] == 'TEST005' for r in models.get_pending_mappings())


def test_upsert_mapping_updates_ignored_row(empty_db_with_user):
    """After mig-112 (one row per bsn_code, no bsn_unit override):
    calling upsert_mapping on a previously-ignored code updates that single
    row to mapped (product_id set, is_ignored cleared by ON CONFLICT update).
    There is no separate 'scoped' row — the ignore is overridden by the remap.
    """
    empty_db, uid = empty_db_with_user
    import models

    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping
            (bsn_code, bsn_name, product_id, is_ignored, ignore_reason)
        VALUES ('TEST007', 'test 007', NULL, 1, 'ค่าขนส่ง')
    """)
    cur = conn.execute("INSERT INTO products (product_name, unit_type) VALUES ('existing', 'ตัว')")
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.commit()
    conn.close()

    models.upsert_mapping('TEST007', 'test 007', product_id=pid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT product_id, is_ignored FROM product_code_mapping WHERE bsn_code='TEST007'"
    ).fetchall()
    conn.close()

    # Exactly one row; it is now mapped (product_id set)
    assert len(rows) == 1
    assert rows[0]['product_id'] == pid
    assert rows[0]['is_ignored'] == 0


def test_upsert_mapping_clears_pending_placeholder(empty_db_with_user):
    """Mapping a pending (product_id=NULL) row via upsert_mapping removes it
    from get_pending_mappings (the ON CONFLICT update sets product_id).
    After mig-112: no bsn_unit column; upsert is by bsn_code only."""
    empty_db, uid = empty_db_with_user
    import models

    conn = sqlite3.connect(empty_db)
    conn.execute("""
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)
        VALUES ('TEST006', 'test 006', NULL)
    """)
    # Need an existing product to map to
    cur = conn.execute("INSERT INTO products (product_name, unit_type) VALUES ('existing', 'ตัว')")
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.commit()
    conn.close()

    models.upsert_mapping('TEST006', 'test 006', product_id=pid)

    assert not any(r['bsn_code'] == 'TEST006' for r in models.get_pending_mappings())


def test_approve_writes_category_id_from_edit(empty_db_with_user):
    """The approve form's category picker resolves to a category_id and sends
    it as an edit → the created product must carry that category_id.

    Regression: approve_pending_suggestion never wrote category_id, so every
    new SKU created through the mapping flow landed with category_id NULL
    (the ทินเนอร์ pid 2020 bug, 2026-06-15)."""
    empty_db, uid = empty_db_with_user
    import models
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "INSERT INTO categories (code, name_th, sort_order) VALUES ('glue','กาว / ซิลิโคน',140)"
    )
    cat_id = conn.execute("SELECT id FROM categories WHERE code='glue'").fetchone()[0]
    conn.commit()
    conn.close()

    sid = _stage_minimal(_stage_payload('CAT001'), user_id=uid)
    new_pid = models.approve_pending_suggestion(
        sid, edits={'category_id': cat_id}, reviewer_id=uid
    )

    conn = sqlite3.connect(empty_db)
    row = conn.execute(
        "SELECT category_id FROM products WHERE id = ?", (new_pid,)
    ).fetchone()
    conn.close()
    assert row[0] == cat_id


def test_approve_resolves_staged_category_text_to_id(empty_db_with_user):
    """The Suggest modal stages `category` as a Thai category name (the
    datalist value). When the approver doesn't override it, approve must
    resolve that text → categories.id so the product is still categorised."""
    empty_db, uid = empty_db_with_user
    import models
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "INSERT INTO categories (code, name_th, sort_order) VALUES ('hinge','บานพับ',30)"
    )
    cat_id = conn.execute("SELECT id FROM categories WHERE code='hinge'").fetchone()[0]
    conn.commit()
    conn.close()

    payload = _stage_payload('CAT002')
    payload['category'] = 'บานพับ'
    sid = _stage_minimal(payload, user_id=uid)
    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    row = conn.execute(
        "SELECT category_id FROM products WHERE id = ?", (new_pid,)
    ).fetchone()
    conn.close()
    assert row[0] == cat_id


def test_approve_unmatched_category_text_leaves_null(empty_db_with_user):
    """Staged category text matching no category row must not crash and must
    leave category_id NULL (no silent bogus category, no new category row)."""
    empty_db, uid = empty_db_with_user
    import models
    payload = _stage_payload('CAT003')
    payload['category'] = 'ไม่มีหมวดนี้จริง'
    sid = _stage_minimal(payload, user_id=uid)
    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    row = conn.execute(
        "SELECT category_id FROM products WHERE id = ?", (new_pid,)
    ).fetchone()
    conn.close()
    assert row[0] is None


def test_approve_generates_sku_code_and_stamps_created_via(empty_db_with_user):
    """Regression (P3, product-creation-consolidation): approve used to
    leave sku_code NULL on every product it created. It now routes through
    models.create_structured_product, which always (re)generates sku_code
    and stamps created_via='smart_mapping'."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload('TEST008'), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sku_code, created_via FROM products WHERE id = ?",
        (new_pid,),
    ).fetchone()
    conn.close()

    assert row['sku_code'] is not None
    assert row['created_via'] == 'smart_mapping'


def test_approve_rolls_back_atomically_when_post_create_step_fails(empty_db_with_user, monkeypatch):
    """Atomicity regression (P3 follow-up fix): create_structured_product used
    to open its OWN connection and commit independently of approve's
    surrounding writes, so a failure in a later step (mapping upsert, status
    update, resolve_pending_mappings) — AFTER the product row had already
    committed on its own — would leave an orphan product with no mapping and
    a suggestion stuck 'pending' forever. create_structured_product now
    accepts approve's own `conn` and does not commit/rollback/close on it,
    so a failure anywhere in approve's transaction rolls back EVERYTHING,
    including the just-inserted product/stock_levels rows."""
    empty_db, uid = empty_db_with_user
    import models

    def _boom(*a, **k):
        raise RuntimeError('boom')

    monkeypatch.setattr(models, 'resolve_pending_mappings', _boom)

    conn = sqlite3.connect(empty_db)
    before_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    before_mapping = conn.execute("SELECT COUNT(*) FROM product_code_mapping").fetchone()[0]
    before_stock = conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0]
    conn.close()

    sid = _stage_minimal(_stage_payload('TEST009'), user_id=uid)

    with pytest.raises(RuntimeError, match='boom'):
        models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    after_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    after_mapping = conn.execute("SELECT COUNT(*) FROM product_code_mapping").fetchone()[0]
    after_stock = conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0]
    sug = conn.execute(
        "SELECT status FROM pending_product_suggestions WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()

    assert after_products == before_products, "no orphan product row must survive a rollback"
    assert after_mapping == before_mapping, "no orphan product_code_mapping row"
    assert after_stock == before_stock, "no orphan stock_levels row"
    assert sug['status'] == 'pending', "suggestion must remain pending, not stuck half-approved"


def test_approve_falls_back_to_catchall_when_bsn_unit_missing(empty_db_with_user):
    """A suggestion with no bsn_unit (no purchase/sale history) still maps the
    code to its new product (mig 112: one row per bsn_code, no unit scoping)."""
    empty_db, uid = empty_db_with_user
    import models
    sid = _stage_minimal(_stage_payload('TEST004', bsn_unit=None), user_id=uid)

    new_pid = models.approve_pending_suggestion(sid, edits={}, reviewer_id=uid)

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code = ?",
        ('TEST004',),
    ).fetchone()
    conn.close()

    assert row['product_id'] == new_pid
