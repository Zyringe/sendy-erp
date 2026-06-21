"""Tests for name_builder.rebuild_product_name — the single-product canonical
name rebuild used by the Master Naming workbench inline editor (Phase 2).

It composes the canonical display name from a product's structured columns via
the shared build() in scripts/build_name_from_columns.py (same logic as the
offline CSV rebuild), joining brands.name + color_finish_codes.name_th.
"""
import sqlite3

import pytest

import name_builder


@pytest.fixture
def one_product(empty_db):
    """A Sendai กลอน with full structured columns + its brand and color row."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    bid = conn.execute(
        "INSERT INTO brands(code, name, name_th, short_code) "
        "VALUES ('sendai','Sendai','เซ็นได','SD')"
    ).lastrowid
    conn.execute("INSERT INTO color_finish_codes(code, name_th) VALUES ('AC','สีรมดำ')")
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size, "
        "                     color_code, packaging_th, sku_code) "
        "VALUES ('ชื่อเดิม', ?, 'กลอน', '#230', '4in', 'AC', 'แผง', 'OLD-SKU')",
        (bid,),
    ).lastrowid
    conn.commit()
    conn.close()
    return empty_db, pid


def test_rebuild_composes_canonical_name_from_columns(one_product):
    path, pid = one_product
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    name = name_builder.rebuild_product_name(conn, pid)
    conn.close()
    assert name == "กลอน Sendai #230-4in สีรมดำ (AC) (แผง)"


def test_rebuild_returns_none_for_missing_product(empty_db):
    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    assert name_builder.rebuild_product_name(conn, 999999) is None
    conn.close()


def test_preview_name_from_proposed_fields(empty_db):
    """Live preview builds the name from not-yet-saved field values, resolving
    brand name + Thai color word from the proposed brand_id / color_code."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    bid = conn.execute(
        "INSERT INTO brands(code, name, name_th, short_code) "
        "VALUES ('s','Sendai','เซ็นได','SD')"
    ).lastrowid
    conn.execute("INSERT INTO color_finish_codes(code, name_th) VALUES ('CR','สีโครเมียม')")
    conn.commit()
    conn.row_factory = sqlite3.Row
    name = name_builder.preview_name(conn, {
        "brand_id": bid, "sub_category": "กลอน", "model": "#230",
        "size": "6in", "color_code": "CR", "packaging_th": "ตัว",
    })
    conn.close()
    assert name == "กลอน Sendai #230-6in สีโครเมียม (CR) (ตัว)"


def test_rebuild_omits_empty_segments(empty_db):
    """No brand / no packaging / no condition → those segments are dropped."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO color_finish_codes(code, name_th) VALUES ('CR','สีโครเมียม')")
    pid = conn.execute(
        "INSERT INTO products(product_name, sub_category, model, size, color_code, sku_code) "
        "VALUES ('x', 'มือจับ', '#5', '6in', 'CR', 'S2')"
    ).lastrowid
    conn.commit()
    conn.row_factory = sqlite3.Row
    name = name_builder.rebuild_product_name(conn, pid)
    conn.close()
    assert name == "มือจับ #5-6in สีโครเมียม (CR)"
