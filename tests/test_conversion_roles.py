"""models/conversion_roles.py — the single place every writer/reader agrees
on what conversion_formula_inputs.role (mig 158) means.

Scope, per the design doc (docs/plans/2026-08-14-hammer-pack-bundle-plan.md
Phase 0 table + §9): applies ONLY to active formulas whose name starts with
'[แพ็ค]'. Everything else ([แกะ] halves, the inactive fid-126-shaped combo
marker, any future N-input manufacturing recipe) is explicitly NOT
interpreted by this module — that is a policy decision this task has no
business making, not an oversight.

Two test styles, both pure/offline (no live DB, no migration 158 dependency):
  - plain-dict unit tests exercising the validator/derivation logic directly
  - a DB-shaped test that builds its OWN two-table sqlite schema (including
    `role`) in a tmp file, to prove the module works against real Row objects
    and, critically, that row ORDER never carries meaning (conversions.py /
    bsn_sync.py read inputs with no ORDER BY, and the table has no index at
    all — see the design doc §3 "Conversion data (prod)").
"""
import sqlite3

import pytest

from models.conversion_roles import (
    ROLE_COMPONENT,
    ROLE_PACKAGING,
    ConversionRoleError,
    component_product_id,
    is_pack_formula,
    validate_pack_inputs,
)


# ─────────────────────────────────────────────────────────────────────────
# is_pack_formula
# ─────────────────────────────────────────────────────────────────────────

def test_is_pack_formula_true_for_active_pack_prefix():
    assert is_pack_formula('[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)', True) is True


def test_is_pack_formula_false_when_inactive():
    assert is_pack_formula('[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)', False) is False


def test_is_pack_formula_false_for_unpack_prefix():
    assert is_pack_formula('[แกะ] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01', True) is False


def test_is_pack_formula_false_for_no_prefix():
    assert is_pack_formula('ชุดฝาครอบลูกบิด', True) is False


# ─────────────────────────────────────────────────────────────────────────
# validate_pack_inputs — plain dicts
# ─────────────────────────────────────────────────────────────────────────

def test_legacy_single_input_role_null_is_valid():
    inputs = [{'product_id': 270, 'quantity': 1, 'role': None}]
    # must not raise
    validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_single_input_role_component_is_valid():
    inputs = [{'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT}]
    validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_single_input_role_packaging_raises():
    """A lone input row is the component (single-input pair-half shape) —
    'packaging' there is a configuration error, not a silently accepted
    third shape."""
    inputs = [{'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING}]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_valid_bundle_two_inputs():
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
    ]
    validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_valid_bundle_reverse_order_still_valid():
    """The whole reason this module exists: row order must never carry
    meaning. Same two rows, packaging first — must validate identically."""
    inputs = [
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
    ]
    validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_no_roles_raises():
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': None},
        {'product_id': 869, 'quantity': 1, 'role': None},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_two_components_raises():
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 271, 'quantity': 1, 'role': ROLE_COMPONENT},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_two_packaging_raises():
    inputs = [
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 870, 'quantity': 1, 'role': ROLE_PACKAGING},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_one_component_two_packaging_raises():
    """Isolates the packaging-count check from the component-count check:
    exactly 1 component (satisfies that clause) + 2 packaging + 0 unroled.
    test_multi_input_two_packaging_raises above is confounded (0 components
    there too, so the component-count clause alone would already catch it)
    — this is the one that actually exercises 'packaging != 1'."""
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 870, 'quantity': 1, 'role': ROLE_PACKAGING},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_valid_pair_plus_stray_unroled_third_row_raises():
    """1 component + 1 packaging + 1 EXTRA row with no role at all. A count
    check that only asserts 'exactly one component AND exactly one
    packaging' (ignoring leftover rows) would wrongly accept this — the
    unroled-rows check is what closes that hole."""
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 999, 'quantity': 1, 'role': None},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_multi_input_three_rows_one_each_plus_extra_component_raises():
    """Guard against a >2-row multi-input [แพ็ค] sneaking a component/packaging
    pair past a naive 'has exactly one of each' check that ignores extras."""
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 271, 'quantity': 1, 'role': ROLE_COMPONENT},
    ]
    with pytest.raises(ConversionRoleError):
        validate_pack_inputs('[แพ็ค] pack', True, inputs)


def test_unpack_formula_not_interpreted():
    """A [แกะ] half is out of scope by construction — even a role-less
    multi-input shape must NOT raise, because this validator has no opinion
    on it at all."""
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': None},
        {'product_id': 869, 'quantity': 1, 'role': None},
    ]
    assert validate_pack_inputs('[แกะ] loose', True, inputs) is None


def test_inactive_pack_formula_not_interpreted():
    """An inactive [แพ็ค] (e.g. mid-edit, or historical) is out of scope —
    matches the fid-126-shaped combo marker's is_active=0 condition."""
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': None},
        {'product_id': 869, 'quantity': 1, 'role': None},
    ]
    assert validate_pack_inputs('[แพ็ค] pack', False, inputs) is None


def test_inactive_combo_marker_shape_not_interpreted():
    """fid-126-shaped: inactive, 2 inputs, no [แพ็ค]/[แกะ] prefix at all
    (marketplace_match._combo_components reads exactly this shape)."""
    inputs = [
        {'product_id': 501, 'quantity': 1, 'role': None},
        {'product_id': 502, 'quantity': 1, 'role': None},
    ]
    assert validate_pack_inputs('ชุดฝาครอบลูกบิด', False, inputs) is None


def test_non_pack_non_unpack_active_formula_not_interpreted():
    """A future active multi-component manufacturing recipe with no
    [แพ็ค]/[แกะ] prefix must not be silently outlawed by this validator —
    deliberately not keyed on len(inputs) > 1."""
    inputs = [
        {'product_id': 501, 'quantity': 1, 'role': None},
        {'product_id': 502, 'quantity': 1, 'role': None},
        {'product_id': 503, 'quantity': 1, 'role': None},
    ]
    assert validate_pack_inputs('ประกอบชุดล็อค', True, inputs) is None


def test_sqlite_row_accepted_not_just_dict():
    """Callers use both sqlite3.Row and plain dicts — accept Row too."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE t (product_id INTEGER, quantity INTEGER, role TEXT)')
    conn.execute("INSERT INTO t VALUES (270, 1, 'component')")
    conn.execute("INSERT INTO t VALUES (869, 1, 'packaging')")
    rows = conn.execute('SELECT product_id, quantity, role FROM t').fetchall()
    conn.close()
    validate_pack_inputs('[แพ็ค] pack', True, rows)


# ─────────────────────────────────────────────────────────────────────────
# component_product_id — the partner lookup cross_unit_hazard will consume
# ─────────────────────────────────────────────────────────────────────────

def test_component_product_id_single_input_role_null():
    inputs = [{'product_id': 270, 'quantity': 1, 'role': None}]
    assert component_product_id('[แพ็ค] pack', True, inputs) == 270


def test_component_product_id_single_input_role_component():
    inputs = [{'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT}]
    assert component_product_id('[แพ็ค] pack', True, inputs) == 270


def test_component_product_id_multi_input_returns_component_row():
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
    ]
    assert component_product_id('[แพ็ค] pack', True, inputs) == 270


def test_component_product_id_reverse_row_order_same_result():
    inputs = [
        {'product_id': 869, 'quantity': 1, 'role': ROLE_PACKAGING},
        {'product_id': 270, 'quantity': 1, 'role': ROLE_COMPONENT},
    ]
    assert component_product_id('[แพ็ค] pack', True, inputs) == 270


def test_component_product_id_raises_on_invalid_shape():
    inputs = [
        {'product_id': 270, 'quantity': 1, 'role': None},
        {'product_id': 869, 'quantity': 1, 'role': None},
    ]
    with pytest.raises(ConversionRoleError):
        component_product_id('[แพ็ค] pack', True, inputs)


def test_component_product_id_raises_for_non_pack_formula():
    """component_product_id has one job: give cross_unit_hazard a partner id
    for a pack formula. Asking it about an unpack/non-pack formula is a
    caller bug — this must raise, not silently return something."""
    inputs = [{'product_id': 270, 'quantity': 1, 'role': None}]
    with pytest.raises(ConversionRoleError):
        component_product_id('[แกะ] loose', True, inputs)


# ─────────────────────────────────────────────────────────────────────────
# DB-shaped test — own tmp-file sqlite schema, mirrors mig 158's shape,
# independent of the live DB / migration runner / the other agent's work.
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def bundle_db(tmp_path):
    db_path = tmp_path / "conv_roles_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE conversion_formulas (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            output_product_id INTEGER NOT NULL,
            output_qty INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE conversion_formula_inputs (
            id INTEGER PRIMARY KEY,
            formula_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            role TEXT CHECK (role IS NULL OR role IN ('component','packaging'))
        );
    """)
    conn.commit()
    yield conn
    conn.close()


def _inputs_for(conn, formula_id):
    """Deliberately NOT ordered — mirrors the real readers this module
    exists to make order-independent."""
    return conn.execute(
        "SELECT product_id, quantity, role FROM conversion_formula_inputs WHERE formula_id = ?",
        (formula_id,)).fetchall()


def test_db_shaped_bundle_reverse_insert_order_same_component(bundle_db):
    conn = bundle_db
    conn.execute(
        "INSERT INTO conversion_formulas (id, name, output_product_id, output_qty, is_active) "
        "VALUES (1, ?, 268, 1, 1)",
        ('[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)',))
    # packaging row inserted BEFORE component row — proves rowid/order does
    # not decide the outcome.
    conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (1, 869, 1, 'packaging')")
    conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (1, 270, 1, 'component')")
    conn.commit()

    formula = conn.execute(
        "SELECT name, is_active FROM conversion_formulas WHERE id = 1").fetchone()
    inputs = _inputs_for(conn, 1)
    assert len(inputs) == 2

    validate_pack_inputs(formula['name'], bool(formula['is_active']), inputs)
    assert component_product_id(formula['name'], bool(formula['is_active']), inputs) == 270


def test_db_shaped_fid126_style_inactive_combo_not_interpreted(bundle_db):
    conn = bundle_db
    conn.execute(
        "INSERT INTO conversion_formulas (id, name, output_product_id, output_qty, is_active) "
        "VALUES (126, 'ชุดฝาครอบลูกบิด', 900, 1, 0)")
    conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (126, 901, 1, NULL)")
    conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (126, 902, 1, NULL)")
    conn.commit()

    formula = conn.execute(
        "SELECT name, is_active FROM conversion_formulas WHERE id = 126").fetchone()
    inputs = _inputs_for(conn, 126)
    assert len(inputs) == 2
    assert validate_pack_inputs(formula['name'], bool(formula['is_active']), inputs) is None


def test_db_shaped_check_constraint_rejects_bogus_role(bundle_db):
    """Sanity: the schema we're testing against actually enforces the same
    CHECK mig 158 will add — proves this fixture is a faithful mirror."""
    conn = bundle_db
    conn.execute(
        "INSERT INTO conversion_formulas (id, name, output_product_id, output_qty, is_active) "
        "VALUES (1, '[แพ็ค] x', 268, 1, 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
            "VALUES (1, 270, 1, 'bogus')")
