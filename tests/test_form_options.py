"""Unit tests for `form_options.py` — the one source of truth for the
page-level pick-list option builders shared by /mapping, /products/new and
/naming (plan `mapping-suggest-clone` PR1).

Every fixture FORCES the rows it asserts on (empty_db_conn carries the live
schema with zero rows) — never relies on whatever happens to be in the dev DB.
"""
import re
import sqlite3

import pytest

import form_options
from sku_code_utils import CONDITION_SHORT, PACKAGING_SHORT


def _insert_product(conn, name, sku_code, *, condition=None):
    conn.execute(
        "INSERT INTO products(product_name, sku_code, condition, is_active) "
        "VALUES (?, ?, ?, 1)",
        (name, sku_code, condition),
    )


# ── packaging() ──────────────────────────────────────────────────────────────

def test_packaging_matches_the_check_trigger(empty_db_conn):
    """packaging() must equal exactly what products_packaging_th_check_insert
    allows — read the trigger from the DB, not a hand-copied list, so this
    fails the moment either side drifts."""
    row = empty_db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='products_packaging_th_check_insert'"
    ).fetchone()
    assert row is not None, "trigger not found — did the schema change?"
    m = re.search(r"NOT IN \((.*?)\)\s*BEGIN", row[0], re.DOTALL)
    assert m, "could not locate the NOT IN (...) clause in the trigger SQL"
    allowed = re.findall(r"'([^']*)'", m.group(1))

    assert len(allowed) == 11, allowed  # control: the trigger clause parsed at all
    assert set(form_options.packaging()) == set(allowed)
    assert form_options.packaging() == list(PACKAGING_SHORT.keys())


# ── conditions() ─────────────────────────────────────────────────────────────

def test_conditions_canonical_first_in_defined_order_then_sorted_extras(empty_db_conn):
    conn = empty_db_conn
    # Extras inserted in an order that would NOT survive if the function
    # merely re-sorted everything alphabetically.
    _insert_product(conn, 'สินค้าทดสอบ 1', 'ZZ-FO-1', condition='แบบมิล')
    _insert_product(conn, 'สินค้าทดสอบ 2', 'ZZ-FO-2', condition='แบบหุล')
    # A canonical value already in CONDITION_SHORT must NOT be duplicated as an extra.
    _insert_product(conn, 'สินค้าทดสอบ 3', 'ZZ-FO-3', condition='ตำหนิ')
    conn.commit()

    result = form_options.conditions(conn)

    canonical = list(CONDITION_SHORT)
    assert result[:len(canonical)] == canonical, "canonical order must be preserved verbatim"
    assert result.count('ตำหนิ') == 1, "a canonical value stored on a product must not duplicate"
    assert result[len(canonical):] == ['แบบมิล', 'แบบหุล'], "extras must be the DB-only values, sorted"


def test_conditions_drop_dated_drops_exp_but_keeps_undated_and_the_canonical_expired(empty_db_conn):
    conn = empty_db_conn
    _insert_product(conn, 'สินค้าทดสอบ EXP 1', 'ZZ-FO-EXP1', condition='EXP:04/2019')
    _insert_product(conn, 'สินค้าทดสอบ EXP 2', 'ZZ-FO-EXP2', condition='EXP:07/2027')
    _insert_product(conn, 'สินค้าทดสอบ หุล', 'ZZ-FO-HUL', condition='แบบหุล')
    _insert_product(conn, 'สินค้าทดสอบ มิล', 'ZZ-FO-MIL', condition='แบบมิล')
    conn.commit()

    default = form_options.conditions(conn)
    assert 'EXP:04/2019' in default and 'EXP:07/2027' in default  # control: extras present by default

    dropped = form_options.conditions(conn, drop_dated=True)
    assert 'EXP:04/2019' not in dropped
    assert 'EXP:07/2027' not in dropped
    assert 'แบบหุล' in dropped and 'แบบมิล' in dropped, "non-dated extras must survive drop_dated"
    assert 'หมดอายุ' in dropped, "the canonical undated 'หมดอายุ' must never be dropped"


def test_conditions_drop_dated_is_keyword_only():
    with pytest.raises(TypeError):
        form_options.conditions(None, True)  # positional — must be rejected


# ── smoke tests for the straight-passthrough builders ────────────────────────
# Cheap insurance against a column/table-name typo that would only surface
# when a template tries to read the attribute (Jinja swallows AttributeError
# as Undefined in some configs, so a route-render test alone can miss this).

def test_brands_colors_categories_units_return_expected_columns(empty_db_conn):
    conn = empty_db_conn
    conn.execute(
        "INSERT INTO brands(code, name, name_th, is_own_brand, sort_order) "
        "VALUES ('ZZBR', 'ZZ Brand', 'แบรนด์ทดสอบ', 1, 1)"
    )
    conn.execute(
        "INSERT INTO color_finish_codes(code, name_th, sort_order) VALUES ('ZZ', 'สีทดสอบ', 1)"
    )
    conn.execute(
        "INSERT INTO categories(code, name_th, sort_order) VALUES ('ZZC', 'หมวดทดสอบ', 1)"
    )
    _insert_product(conn, 'สินค้าทดสอบหน่วย', 'ZZ-FO-UNIT')
    conn.execute("UPDATE products SET unit_type='ชิ้น' WHERE sku_code='ZZ-FO-UNIT'")
    conn.commit()

    b = form_options.brands(conn)
    assert any(r['name_th'] == 'แบรนด์ทดสอบ' for r in b)

    c = form_options.colors(conn)
    assert any(r['name_th'] == 'สีทดสอบ' for r in c)

    cat = form_options.categories(conn)
    assert any(r['name_th'] == 'หมวดทดสอบ' for r in cat)

    u = form_options.units(conn)
    assert 'ชิ้น' in u
