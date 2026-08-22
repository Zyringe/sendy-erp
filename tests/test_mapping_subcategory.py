"""PR2 — sub_category / sub_category_short_code / category_id reach the
product (projects/mapping-suggest-clone/plan.md, "PR2 — the naming taxonomy
reaches the product").

Bug being fixed (pid 2044, the one product ever created via
created_via='smart_mapping'): `parse_name` emits a fine-grained "category"
(a products.sub_category shape, e.g. 'แปรงทาสี') but the old Card B combo fed
it into the 39-row `categories` master, which discarded it — the product came
out with a leading space in its name and neither sku_code prefix segment
(sub_category NULL, sub_category_short_code NULL, category_id NULL).

Covers:
  - migration 169 forward/rollback (round-trip on a drop-first fixture)
  - save_pending_suggestion persists all three new columns, INSERT + UPSERT
  - approve_pending_suggestion threads them onto the created product
    (the pid-2044 regression fixture, asserting the actual strings)
  - stage (via the real /mapping/save route) → reload → approve preserves
    category_id — the path that silently drops it today at blueprints/bsn.py

⚠ tests/conftest.py::tmp_db clones the LIVE dev DB *with its data*. Every
fixture below deletes its own bsn_code/product rows before inserting, and
never assumes an empty pending_product_suggestions table.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_169 = os.path.join(_REPO, "data", "migrations",
                         "169_pending_suggestion_subcategory.sql")
_MIG_169_ROLLBACK = os.path.join(_REPO, "data", "migrations",
                                  "169_pending_suggestion_subcategory.rollback.sql")
_FILENAME_169 = "169_pending_suggestion_subcategory.sql"

# A stable, long-lived categories row (id=6, ค้อน) — short_code 'HMR'.
# Picked because category ids 1-10 are seeded in every environment (verified
# live) and unlikely to ever be renumbered/deleted.
_CAT_ID = 6
_CAT_SHORT = 'HMR'

_TEST_BSN_CODE = 'ZZTEST-SUBCAT-01'


def _table_cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _reset_to_pre_169(conn):
    """tmp_db clones the live DB, which already has 169 applied (this branch
    applied it to the shared dev DB during migration authoring) — roll it
    back first so tests start from the true pre-mig state, matching the
    established pattern (see tests/test_mig122_created_via.py)."""
    applied = conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename=?", (_FILENAME_169,)
    ).fetchone()
    if applied is not None:
        with open(_MIG_169_ROLLBACK, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()


@pytest.fixture
def pre169_conn(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _reset_to_pre_169(conn)
    yield conn
    conn.close()


def _clean_row(conn, bsn_code=_TEST_BSN_CODE):
    """Force our own state: wipe any prior test-run leftovers keyed on this
    bsn_code (pending_product_suggestions.bsn_code is UNIQUE) and any product/
    mapping rows a prior approve created."""
    row = conn.execute(
        "SELECT approved_product_id FROM pending_product_suggestions WHERE bsn_code=?",
        (bsn_code,),
    ).fetchone()
    if row and row[0]:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (row[0],))
        conn.execute("DELETE FROM products WHERE id=?", (row[0],))
    conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code=?", (bsn_code,))
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (bsn_code,))
    conn.commit()


# ── Migration 169: forward + rollback round-trip ────────────────────────────

def test_migration_file_exists():
    assert os.path.exists(_MIG_169)
    assert os.path.exists(_MIG_169_ROLLBACK)


def test_pre_state_columns_absent(pre169_conn):
    cols = _table_cols(pre169_conn, 'pending_product_suggestions')
    assert 'sub_category' not in cols
    assert 'sub_category_short_code' not in cols
    assert 'category_id' not in cols


def test_migration_169_adds_columns_and_is_recorded(pre169_conn):
    import database
    ran = database.run_pending_migrations(pre169_conn, verbose=False)
    assert _FILENAME_169 in ran, f"mig 169 should have run; ran={ran}"

    cols = _table_cols(pre169_conn, 'pending_product_suggestions')
    assert 'sub_category' in cols
    assert 'sub_category_short_code' in cols
    assert 'category_id' in cols

    applied = pre169_conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename=?", (_FILENAME_169,)
    ).fetchone()
    assert applied is not None


def test_migration_169_rollback_restores_pre_state(pre169_conn):
    import database
    database.run_pending_migrations(pre169_conn, verbose=False)
    assert 'sub_category' in _table_cols(pre169_conn, 'pending_product_suggestions')

    with open(_MIG_169_ROLLBACK, encoding='utf-8') as f:
        pre169_conn.executescript(f.read())
    pre169_conn.commit()

    cols = _table_cols(pre169_conn, 'pending_product_suggestions')
    assert 'sub_category' not in cols
    assert 'sub_category_short_code' not in cols
    assert 'category_id' not in cols
    applied = pre169_conn.execute(
        "SELECT 1 FROM applied_migrations WHERE filename=?", (_FILENAME_169,)
    ).fetchone()
    assert applied is None, "rollback must un-record the migration"


# ── save_pending_suggestion: INSERT + UPSERT persist all three columns ─────

def _base_stage_payload(bsn_code=_TEST_BSN_CODE, **overrides):
    payload = {
        'bsn_code': bsn_code,
        'bsn_name': f'test raw name {bsn_code}',
        'suggested_name': None,
        'category': 'แปรงทาสี',       # legacy free-text column, kept as-is
        'sub_category': 'แปรงทาสี',
        'sub_category_short_code': 'BPNT',
        'category_id': _CAT_ID,
        'series': None, 'brand_id': None,
        'model': None, 'size': None, 'color_th': None, 'color_code': None,
        'packaging': None, 'condition': None, 'pack_variant': None,
        'suggested_cost': 0.0, 'suggested_unit_type': 'ตัว',
        'units_per_carton': None, 'units_per_box': None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def db169(tmp_db):
    """tmp_db with 169 guaranteed applied (idempotent no-op if already there)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    import database
    database.run_pending_migrations(conn, verbose=False)
    _clean_row(conn)
    conn.close()
    return tmp_db


def test_save_pending_suggestion_persists_subcategory_fields(db169):
    import models
    _sid = models.save_pending_suggestion(_base_stage_payload(), user_id=1)

    conn = sqlite3.connect(db169)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sub_category, sub_category_short_code, category_id "
        "FROM pending_product_suggestions WHERE bsn_code=?",
        (_TEST_BSN_CODE,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['sub_category'] == 'แปรงทาสี'
    assert row['sub_category_short_code'] == 'BPNT'
    assert row['category_id'] == _CAT_ID


def test_save_pending_suggestion_upsert_second_stage_wins(db169):
    import models
    models.save_pending_suggestion(_base_stage_payload(), user_id=1)
    models.save_pending_suggestion(
        _base_stage_payload(sub_category='ลูกบิด',
                             sub_category_short_code='KNBSUB',
                             category_id=2),
        user_id=1,
    )

    conn = sqlite3.connect(db169)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sub_category, sub_category_short_code, category_id "
        "FROM pending_product_suggestions WHERE bsn_code=?",
        (_TEST_BSN_CODE,),
    ).fetchall()
    conn.close()
    # COUNT first (vacuity guard) — the UPSERT must not leave two rows.
    assert len(rows) == 1
    row = rows[0]
    assert row['sub_category'] == 'ลูกบิด'
    assert row['sub_category_short_code'] == 'KNBSUB'
    assert row['category_id'] == 2


# ── approve_pending_suggestion → created product (pid-2044 regression) ─────

def test_approve_creates_product_with_subcategory_and_category_id(db169):
    """The exact defect pid 2044 shipped with: sub_category/short_code/
    category_id all NULL. Fix = all three land on the created product, and
    sku_code carries both prefix segments (cat_short_code + sub_category
    short code)."""
    import models
    sid = models.save_pending_suggestion(_base_stage_payload(), user_id=1)
    new_pid = models.approve_pending_suggestion(sid, {}, reviewer_id=1)

    conn = sqlite3.connect(db169)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT product_name, sku_code, sub_category, sub_category_short_code, "
        "category_id FROM products WHERE id=?", (new_pid,)
    ).fetchone()
    conn.close()

    assert row['sub_category'] == 'แปรงทาสี'
    assert row['sub_category_short_code'] == 'BPNT'
    assert row['category_id'] == _CAT_ID
    assert row['sku_code'].startswith(_CAT_SHORT), \
        f"sku_code missing cat_short_code prefix: {row['sku_code']!r}"
    assert 'BPNT' in row['sku_code'], \
        f"sku_code missing sub_category_short_code segment: {row['sku_code']!r}"


def test_derived_name_uses_subcategory_as_first_segment_no_leading_space(db169):
    """The "leading space, first name segment empty" half of the pid-2044
    symptom traces to create_structured_product's DERIVE-FROM-COLUMNS path
    (name_builder.rebuild_product_name), which reads products.sub_category as
    the name's first segment. That path is exercised directly here because
    approve_pending_suggestion always supplies a non-empty product_name
    override (suggested_name or, failing that, the NOT NULL bsn_name) — so it
    can never itself reach the derive path; this pins the mechanism at the
    layer that actually implements it."""
    from models.products import create_structured_product
    pid = create_structured_product({
        'product_name': None,   # falsy -> derive from spec columns
        'sub_category': 'แปรงทาสี',
        'sub_category_short_code': 'BPNT',
        'category_id': _CAT_ID,
    }, 'smart_mapping')

    conn = sqlite3.connect(db169)
    row = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    name = row[0]
    assert name.startswith('แปรงทาสี'), \
        f"sub_category should be the name's first segment: {name!r}"
    assert not name.startswith(' '), f"leading-space artifact: {name!r}"


# ── stage (real route) → reload → approve preserves category_id ────────────

@pytest.fixture
def manager_client(db169):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c, db169


def test_stage_via_route_reload_approve_preserves_category_id(manager_client):
    """This is the path that silently drops category_id today: mapping.html's
    confirmStageNew() already sends it, but blueprints/bsn.py's stage payload
    must forward it into save_pending_suggestion for it to survive."""
    client, db_path = manager_client
    resp = client.post('/mapping/save', json={
        'mappings': [{
            'bsn_code': _TEST_BSN_CODE,
            'bsn_name': 'ทดสอบ subcat route',
            'action': 'stage',
            'suggested_name': None,
            'category': 'แปรงทาสี',
            'category_id': _CAT_ID,
            'sub_category': 'แปรงทาสี',
            'sub_category_short_code': 'BPNT',
            'series': None, 'brand_id': None, 'model': None, 'size': None,
            'color_th': None, 'color_code': None, 'packaging': None,
            'condition': None, 'pack_variant': None,
            'suggested_cost': 0, 'suggested_unit_type': 'ตัว',
            'units_per_carton': None, 'units_per_box': None,
        }],
    })
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True

    # Reload from the DB (a fresh connection — the real "closed browser tab,
    # manager comes back later" scenario) and confirm category_id survived
    # the round-trip through the route, not just through models directly.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    staged = conn.execute(
        "SELECT id, category_id, sub_category, sub_category_short_code "
        "FROM pending_product_suggestions WHERE bsn_code=?",
        (_TEST_BSN_CODE,),
    ).fetchone()
    conn.close()
    assert staged is not None
    assert staged['category_id'] == _CAT_ID, \
        "category_id dropped at the stage route (blueprints/bsn.py forwarding bug)"
    assert staged['sub_category'] == 'แปรงทาสี'
    assert staged['sub_category_short_code'] == 'BPNT'

    resp2 = client.post(f'/mapping/suggestions/{staged["id"]}/approve', json={})
    assert resp2.status_code == 200
    body = resp2.get_json()
    assert body['ok'] is True
    new_pid = body['product_id']

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prod = conn.execute(
        "SELECT sku_code, category_id FROM products WHERE id=?", (new_pid,)
    ).fetchone()
    conn.close()
    assert prod['category_id'] == _CAT_ID
    assert prod['sku_code'].startswith(_CAT_SHORT), \
        f"created sku_code missing cat_short_code prefix: {prod['sku_code']!r}"


# ── Render guard: the new fields actually reach both HTML surfaces ─────────
# (curl/pytest sees markup only, not the live-browser prefill/JS — see the
# Chrome MCP click-through in the final report for that half.)

@pytest.fixture
def render_client(db169):
    conn = sqlite3.connect(db169)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored) "
        "VALUES (?, 'combo render test', NULL, 0)", (_TEST_BSN_CODE,)
    )
    cur = conn.execute(
        "INSERT INTO pending_product_suggestions "
        "(bsn_code, bsn_name, status, created_at, sub_category, sub_category_short_code, category_id) "
        "VALUES (?, 'combo suggestion test', 'pending', datetime('now'), 'แปรงทาสี', 'BPNT', ?)",
        (_TEST_BSN_CODE + '-SUG', _CAT_ID),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c, sid


def test_suggest_modal_renders_subcategory_fields(render_client):
    client, _sid = render_client
    body = client.get('/mapping').get_data(as_text=True)
    assert 'id="sm-subcat"' in body
    assert 'id="sm-subcat-short"' in body


def test_approve_form_renders_subcategory_fields_and_prefill(render_client):
    client, sid = render_client
    body = client.get('/mapping?tab=suggestions').get_data(as_text=True)
    assert f'id="sug-subcat-{sid}"' in body
    assert f'value="แปรงทาสี"' in body
    assert f'id="sug-subcat-short-{sid}"' in body
    assert 'value="BPNT"' in body
    # category_id prefill reaches the hidden combo-value input for THIS row
    # specifically — the exact wiring that was missing before (the id was
    # rendered with no value= attribute at all pre-fix).
    assert f'id="sug-cat-id-{sid}" value="{_CAT_ID}"' in body


# ── Codex review 2026-08-22: clearing an FK picker at approval time ──────────

def test_approve_honours_an_explicit_category_id_clear(tmp_db):
    """The Tab-2 approve form sends `v('cat-id') || null`, so clearing the
    Category picker submits an explicit null. That must CLEAR the staged
    category_id, not be discarded as 'not edited' — otherwise the product is
    created in the category the manager just removed, and its sku_code keeps
    that category's short-code prefix."""
    import sqlite3
    import models

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    cat_id = conn.execute(
        "SELECT id FROM categories WHERE short_code IS NOT NULL AND short_code <> '' LIMIT 1"
    ).fetchone()['id']
    conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code='ZZCLEAR01'")
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code='ZZCLEAR01'")
    conn.commit()
    conn.close()

    sid = models.save_pending_suggestion({
        'bsn_code': 'ZZCLEAR01', 'bsn_name': 'clear-test raw',
        'suggested_name': 'clear-test product',
        'category': None, 'category_id': cat_id,
        'sub_category': 'หมวดทดสอบเคลียร์', 'sub_category_short_code': 'ZCLR',
        'series': None, 'brand_id': None, 'model': 'ZZC1', 'size': None,
        'color_th': None, 'color_code': None, 'packaging': None,
        'condition': None, 'pack_variant': None,
        'suggested_cost': 0, 'suggested_unit_type': 'ตัว',
        'units_per_carton': 1, 'units_per_box': 1,
    }, user_id=1)

    # CONTROL: the staged row really does carry the category we are about to clear.
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    staged = conn.execute(
        "SELECT category_id FROM pending_product_suggestions WHERE id=?", (sid,)
    ).fetchone()
    conn.close()
    assert staged['category_id'] == cat_id, "fixture did not stage a category_id"

    # The manager clears the picker -> the form submits an explicit null.
    new_pid = models.approve_pending_suggestion(sid, {'category_id': None}, reviewer_id=1)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT category_id, sku_code FROM products WHERE id=?", (new_pid,)
    ).fetchone()
    short = conn.execute(
        "SELECT short_code FROM categories WHERE id=?", (cat_id,)
    ).fetchone()['short_code']
    conn.close()

    assert row['category_id'] is None, (
        f"the cleared category survived approval as {row['category_id']}"
    )
    assert not row['sku_code'].startswith(short + '-'), (
        f"sku_code {row['sku_code']!r} still carries the cleared category prefix {short!r}"
    )
