"""Tests for naming_cascade.save_product — the Master Naming workbench single-
product inline edit (Phase 2).

Editing a product's structured columns rebuilds product_name (via name_builder)
and regenerates sku_code (via sku_code_utils, lock-aware), under a backup +
BEGIN IMMEDIATE + invariant asserts. Engine-style (direct call) so it's
deterministic in the full suite.
"""
import sqlite3

import pytest

import naming_cascade as nc


@pytest.fixture
def editable_product(empty_db):
    """A Sendai กลอน #230-4in in สีรมดำ (AC), แผง. sku 'SEED-1' (regenerated on save)."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    bid = conn.execute(
        "INSERT INTO brands(code, name, name_th, short_code) "
        "VALUES ('sendai','Sendai','เซ็นได','SD')"
    ).lastrowid
    conn.executescript(
        "INSERT INTO color_finish_codes(code, name_th) VALUES "
        "('AC','สีรมดำ'),('CR','สีโครเมียม');"
    )
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size, "
        "                     color_code, packaging_th, packaging_short, sku_code) "
        "VALUES ('กลอน Sendai #230-4in สีรมดำ (AC) (แผง)', ?, 'กลอน', '#230', '4in', "
        "        'AC', 'แผง', 'PN', 'SEED-1')",
        (bid,),
    ).lastrowid
    conn.commit()
    conn.close()
    return empty_db, pid, bid


def test_save_rebuilds_name_and_sku(editable_product, tmp_path):
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()

    assert row["product_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"
    assert row["sku_code"] == "SD-#230-4in-CR-PN"
    assert res["new_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"
    assert res["old_sku"] == "SEED-1"
    assert res["new_sku"] == "SD-#230-4in-CR-PN"
    assert res["sku_locked_skipped"] is False


def test_save_derives_packaging_short_from_packaging_th(editable_product, tmp_path):
    path, pid, _ = editable_product
    nc.save_product(path, pid, {"packaging_th": "ตัว"}, backup_dir=str(tmp_path / "b"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT product_name, packaging_short, sku_code FROM products WHERE id=?",
        (pid,)).fetchone()
    conn.close()
    assert row["packaging_short"] == "UN"                      # ตัว → UN derived
    assert row["product_name"].endswith("(ตัว)")
    assert row["sku_code"].endswith("-UN")


def test_save_respects_sku_lock(editable_product, tmp_path):
    path, pid, _ = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET sku_code_locked=1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()

    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    # Name still rebuilds; sku_code is frozen because it's locked.
    assert row["product_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"
    assert row["sku_code"] == "SEED-1"
    assert res["sku_locked_skipped"] is True


def test_save_missing_product_raises(empty_db, tmp_path):
    with pytest.raises(nc.ProductNotFound):
        nc.save_product(empty_db, 999999, {"color_code": "CR"},
                        backup_dir=str(tmp_path / "b"))
