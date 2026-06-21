"""Tests for naming_cascade — the dictionary→product_name cascade engine.

The engine NEVER does a full rebuild (that would mass-overwrite hand-tuned
names — 41.6% of decomposed names diverge from build()). It does a targeted
substring replace, scoped by the structured field (color_code / brand_id),
with a preview→confirm→apply flow guarded by a backup + invariant asserts.

Scenarios mirror the real AB/JBB case:
  - color AB = JBB = 'สีทองแดงรมดำ' (two codes share one Thai word)
  - so a string-only scan would wrongly rename JBB products → we MUST scope by code
  - one AB product (#118AB) has the code but not the word → must be skipped
Synthetic rows (ZZA/ZZB, brand ZTEST) reproduce this deterministically against
the real schema (FKs + CHECK triggers active) without depending on live data.
"""
import os
import sqlite3

import pytest

import naming_cascade as nc


# ── pure substring replace ────────────────────────────────────────────────────

def test_replace_token_replaces_when_present():
    assert nc.replace_token(
        "กรอบจตุคาม 5cm สีทองแดงรมดำ (AB) (แผง)", "สีทองแดงรมดำ", "สีทองเหลืองรมดำ"
    ) == ("กรอบจตุคาม 5cm สีทองเหลืองรมดำ (AB) (แผง)", True)


def test_replace_token_skips_when_word_absent():
    # #118AB has the code AB in the model but NOT the color word — must be skipped.
    assert nc.replace_token(
        "ใบเลื่อยจิ๊กซอตัดเหล็ก #118AB", "สีทองแดงรมดำ", "สีทองเหลืองรมดำ"
    ) == ("ใบเลื่อยจิ๊กซอตัดเหล็ก #118AB", False)


def test_replace_token_skips_when_old_equals_new():
    assert nc.replace_token("x สีดำ", "สีดำ", "สีดำ") == ("x สีดำ", False)


# ── fixtures: synthetic, deterministic scenarios ──────────────────────────────

@pytest.fixture
def color_db(empty_db):
    """ZZA & ZZB share the Thai word 'สีทดสอบรวม' (AB/JBB analogue).
    Products: one ZZA-with-word (affected), one ZZA-without-word (skip),
    one ZZB-with-word (must stay untouched — different code)."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        "INSERT INTO color_finish_codes(code, name_th) VALUES "
        "('ZZA','สีทดสอบรวม'),('ZZB','สีทดสอบรวม');"
    )

    def add(name, color, sku):
        cur = conn.execute(
            "INSERT INTO products(product_name, color_code, sku_code) VALUES (?,?,?)",
            (name, color, sku),
        )
        return cur.lastrowid

    ids = {
        "a_word": add("ทดสอบเอ สีทดสอบรวม (ZZA) (แผง)", "ZZA", "ZZTEST-A1"),
        "a_noword": add("ทดสอบเอไม่มีคำ #ZZA", "ZZA", "ZZTEST-A2"),
        "b_word": add("ทดสอบบี สีทดสอบรวม (ZZB) (แผง)", "ZZB", "ZZTEST-B1"),
    }
    conn.commit()
    conn.close()
    return empty_db, ids


@pytest.fixture
def brand_db(empty_db):
    """Brand ZTEST: name='ZTEST', name_th='แซดเทสต์'. Products: one using the
    Thai token (affected when standardizing to English), one already English
    (already_target), one with neither (brand_absent)."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO brands(code, name, name_th, short_code) VALUES "
        "('ZBRAND','ZTEST','แซดเทสต์','ZTS')"
    )
    bid = cur.lastrowid

    def add(name, sku):
        c = conn.execute(
            "INSERT INTO products(product_name, brand_id, sku_code) VALUES (?,?,?)",
            (name, bid, sku),
        )
        return c.lastrowid

    ids = {
        "bid": bid,
        "thai": add("กลอน แซดเทสต์ #100", "ZB-1"),
        "eng": add("กลอน ZTEST #200", "ZB-2"),
        "absent": add("กลอน #300", "ZB-3"),
    }
    conn.commit()
    conn.close()
    return empty_db, ids


# ── color preview: scope-by-code + word-absent skip + shared-word guard ────────

def test_preview_color_scopes_by_code_and_flags_word_absent(color_db):
    path, ids = color_db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    pv = nc.preview(conn, "color", "ZZA", "สีทดสอบใหม่")
    conn.close()

    affected = {a["id"]: a["new_name"] for a in pv["affected"]}
    skipped = {s["id"]: s["reason"] for s in pv["skipped"]}

    assert affected == {ids["a_word"]: "ทดสอบเอ สีทดสอบใหม่ (ZZA) (แผง)"}
    assert skipped == {ids["a_noword"]: "word_absent"}
    # The ZZB product shares the exact Thai word but a DIFFERENT code — it must
    # never enter the AB cascade's candidate set at all.
    assert ids["b_word"] not in affected
    assert ids["b_word"] not in skipped


# ── color apply: updates only scoped names + the dict row, backs up first ──────

def test_apply_color_updates_scoped_names_and_dict_only(color_db, tmp_path):
    path, ids = color_db
    backup_dir = str(tmp_path / "backups")

    res = nc.apply(path, "color", "ZZA", "สีทดสอบใหม่",
                   expected_count=1, backup_dir=backup_dir)

    conn = sqlite3.connect(path)
    name_a = conn.execute("SELECT product_name FROM products WHERE id=?",
                          (ids["a_word"],)).fetchone()[0]
    name_b = conn.execute("SELECT product_name FROM products WHERE id=?",
                          (ids["b_word"],)).fetchone()[0]
    zza = conn.execute("SELECT name_th FROM color_finish_codes WHERE code='ZZA'").fetchone()[0]
    zzb = conn.execute("SELECT name_th FROM color_finish_codes WHERE code='ZZB'").fetchone()[0]
    conn.close()

    assert name_a == "ทดสอบเอ สีทดสอบใหม่ (ZZA) (แผง)"
    assert name_b == "ทดสอบบี สีทดสอบรวม (ZZB) (แผง)"   # shared-word product untouched
    assert zza == "สีทดสอบใหม่"
    assert zzb == "สีทดสอบรวม"                          # JBB-analogue dict row untouched
    assert res["applied"] == 1
    # A backup snapshot must exist before any mutation.
    assert os.path.isdir(backup_dir)
    assert any(f.endswith(".db.gz") for f in os.listdir(backup_dir))


def test_apply_color_leaves_sku_code_untouched(color_db, tmp_path):
    path, ids = color_db
    nc.apply(path, "color", "ZZA", "สีทดสอบใหม่",
             expected_count=1, backup_dir=str(tmp_path / "b"))
    conn = sqlite3.connect(path)
    sku = conn.execute("SELECT sku_code FROM products WHERE id=?",
                       (ids["a_word"],)).fetchone()[0]
    conn.close()
    assert sku == "ZZTEST-A1"


# ── apply rolls back when the affected count drifts from the preview ───────────

def test_apply_rolls_back_on_count_mismatch(color_db, tmp_path):
    path, ids = color_db
    with pytest.raises(nc.CascadeConflict):
        nc.apply(path, "color", "ZZA", "สีทดสอบใหม่",
                 expected_count=5, backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    name_a = conn.execute("SELECT product_name FROM products WHERE id=?",
                          (ids["a_word"],)).fetchone()[0]
    zza = conn.execute("SELECT name_th FROM color_finish_codes WHERE code='ZZA'").fetchone()[0]
    conn.close()
    # Nothing committed: original Thai word + dict value both intact.
    assert "สีทดสอบรวม" in name_a
    assert zza == "สีทดสอบรวม"


def test_apply_rolls_back_when_an_invariant_fails(color_db, tmp_path, monkeypatch):
    """An integrity invariant tripping mid-apply must roll back the whole
    cascade — both the dict row and the product names — not leave a partial."""
    path, ids = color_db
    # Force the sku-uniqueness check to fail after the writes are staged.
    monkeypatch.setattr(nc, "_sku_unique_ok", lambda conn: False)
    with pytest.raises(nc.CascadeInvariantError):
        nc.apply(path, "color", "ZZA", "สีทดสอบใหม่",
                 expected_count=1, backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    name_a = conn.execute("SELECT product_name FROM products WHERE id=?",
                          (ids["a_word"],)).fetchone()[0]
    zza = conn.execute("SELECT name_th FROM color_finish_codes WHERE code='ZZA'").fetchone()[0]
    conn.close()
    assert "สีทดสอบรวม" in name_a   # name update rolled back
    assert zza == "สีทดสอบรวม"      # dict update rolled back too


# ── brand standardize: en/th detection + already-target + brand-absent ─────────

def test_preview_brand_standardize_to_english(brand_db):
    path, ids = brand_db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    pv = nc.preview(conn, "brand", ids["bid"], "ZTEST")
    conn.close()

    affected = {a["id"]: a["new_name"] for a in pv["affected"]}
    skipped = {s["id"]: s["reason"] for s in pv["skipped"]}

    assert affected == {ids["thai"]: "กลอน ZTEST #100"}
    assert skipped == {ids["eng"]: "already_target", ids["absent"]: "brand_absent"}


def test_apply_brand_keeps_brands_table_and_sku_code(brand_db, tmp_path):
    path, ids = brand_db
    res = nc.apply(path, "brand", ids["bid"], "ZTEST",
                   expected_count=1, backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    name = conn.execute("SELECT product_name FROM products WHERE id=?",
                       (ids["thai"],)).fetchone()[0]
    bname_th = conn.execute("SELECT name_th FROM brands WHERE id=?",
                          (ids["bid"],)).fetchone()[0]
    sku = conn.execute("SELECT sku_code FROM products WHERE id=?",
                      (ids["thai"],)).fetchone()[0]
    conn.close()

    assert name == "กลอน ZTEST #100"
    assert bname_th == "แซดเทสต์"   # standardize is name-only: brands table unchanged
    assert sku == "ZB-1"            # sku_code untouched
    assert res["applied"] == 1
