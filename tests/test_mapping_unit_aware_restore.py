"""Migration 124 — restore product_code_mapping.bsn_unit (unit-aware resolver).

TDD spec per projects/pack-loose-sku-split/plan.md Phase 1. Mig 112 (2026-06-09)
dropped bsn_unit; this restores it via a NEW forward migration (mig 124), NOT
by running 112's rollback (that would desync applied_migrations on prod).

`tmp_db` is a straight copy of the LIVE local DB, which does not have mig 124
applied on this machine (implementer session never touches the live DB — see
erp-engineering-discipline.md). Every test here applies 124_restore_mapping_
bsn_unit.sql to its own tmp_db copy before exercising the resolver, mirroring
tests/test_migration_061_mapping_unit_aware.py's pattern.

(a) split rows resolve by exact unit
(b) a code with only a blank bsn_unit row resolves for ANY unit
(c) unit normalization (acronym 'ตว' -> full 'ตัว') hits the split row
(d) an ignored row still reports is_ignored, regardless of unit
(e) regression: non-split (bsn_unit='') mapping resolves identically to a
    pure `WHERE bsn_code=?` lookup — restoring the column changes nothing
    for any code that was never split.
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402

MIG = os.path.join(REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")

PA, PB = 907301, 907302


def _migrate(conn):
    with open(MIG, encoding="utf-8") as f:
        conn.executescript(f.read())


def _seed_products(conn):
    conn.row_factory = sqlite3.Row
    for pid in (PA, PB):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
            "VALUES (?, ?, 'ตัว', ?, 1)",
            (pid, f"P{pid}", f"SK{pid}")
        )
    conn.commit()


# ── (a) split rows resolve by exact unit ─────────────────────────────────────

def test_split_code_resolves_by_exact_unit(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    _seed_products(conn)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('030บ3412','#412 GP',?, 'แผง')", (PA,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('030บ3412','#412 GP',?, 'ตัว')", (PB,))
    conn.commit()

    pid_pack, _, mapped_pack = models._resolve_mapping(conn, '030บ3412', 'แผง')
    pid_loose, _, mapped_loose = models._resolve_mapping(conn, '030บ3412', 'ตัว')

    assert (pid_pack, mapped_pack) == (PA, True)
    assert (pid_loose, mapped_loose) == (PB, True)
    conn.close()


# ── (b) blank-only code resolves for ANY unit ────────────────────────────────

def test_blank_only_code_resolves_for_any_unit(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    _seed_products(conn)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('ZBLANK1','n',?, '')", (PA,))
    conn.commit()

    for unit in ('แผง', 'ตัว', 'กล่อง', ''):
        pid, _, mapped = models._resolve_mapping(conn, 'ZBLANK1', unit)
        assert (pid, mapped) == (PA, True), f"unit={unit!r}"
    conn.close()


# ── (c) normalization: raw acronym hits the full-Thai split row ─────────────

def test_unit_normalization_hits_split_row(tmp_db):
    """Import unit 'ตว' (acronym) must resolve the 'ตัว' (full-Thai) split row
    — bsn_units.normalize_unit is the same helper import_weekly uses."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    _seed_products(conn)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('030บ3412','n',?, 'แผง')", (PA,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('030บ3412','n',?, 'ตัว')", (PB,))
    conn.commit()

    pid, _, mapped = models._resolve_mapping(conn, '030บ3412', 'ตว')  # raw acronym
    assert (pid, mapped) == (PB, True)
    conn.close()


# ── (d) ignored row still reports is_ignored, regardless of unit ────────────

def test_ignored_row_still_reports_ignored(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    _seed_products(conn)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,is_ignored,bsn_unit) "
        "VALUES ('ZIGN1','junk code',1,'')"
    )
    conn.commit()

    pid, is_ignored, mapped = models._resolve_mapping(conn, 'ZIGN1', 'ตัว')
    assert mapped is True
    assert is_ignored == 1
    assert pid is None
    conn.close()


# ── (e) regression: non-split codes behave exactly like a pure bsn_code
#       lookup — restoring the column changes nothing for un-split codes ────

def test_nonsplit_codes_resolve_identically_to_pure_bsn_code_lookup(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    _seed_products(conn)
    codes = [
        ("ZNS1", PA, 0),
        ("ZNS2", PB, 0),
        ("ZNS3", None, 0),   # pending/unmapped
        ("ZNS4", PA, 1),     # ignored
    ]
    for code, pid, ignored in codes:
        conn.execute(
            "INSERT INTO product_code_mapping "
            "(bsn_code,bsn_name,product_id,is_ignored,bsn_unit) "
            "VALUES (?,?,?,?, '')",
            (code, f"name-{code}", pid, ignored)
        )
    conn.commit()

    for code, _expected_pid, _expected_ignored in codes:
        old_style = conn.execute(
            "SELECT product_id, is_ignored FROM product_code_mapping "
            "WHERE bsn_code = ? LIMIT 1", (code,)
        ).fetchone()
        for unit in ('แผง', 'ตัว', 'กล่อง', ''):
            pid, is_ignored, mapped = models._resolve_mapping(conn, code, unit)
            assert pid == old_style['product_id'], f"code={code} unit={unit!r}"
            assert is_ignored == old_style['is_ignored'], f"code={code} unit={unit!r}"
            assert mapped is True
    conn.close()


def test_unknown_code_still_unmapped(tmp_db):
    """Sanity companion to (e): a code with NO row at all stays unresolved,
    same as pre-restore."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    pid, is_ignored, mapped = models._resolve_mapping(conn, 'NO_SUCH_CODE', 'ตัว')
    assert pid is None
    assert mapped is False
    conn.close()


# ── migration mechanics: row/id preservation + composite UNIQUE ─────────────

def test_migration_preserves_rows_and_ids_and_enforces_composite_unique(tmp_db):
    conn = sqlite3.connect(tmp_db)
    pre = conn.execute(
        "SELECT id, bsn_code, bsn_name, product_id, is_ignored, ignore_reason "
        "FROM product_code_mapping ORDER BY id"
    ).fetchall()
    conn.close()

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(product_code_mapping)")}
    assert "bsn_unit" in cols

    post = conn.execute(
        "SELECT id, bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit "
        "FROM product_code_mapping ORDER BY id"
    ).fetchall()
    assert len(post) == len(pre), "row count changed across the migration"
    assert all(r["bsn_unit"] == "" for r in post), "existing rows must default to ''"
    assert [r["id"] for r in post] == [r[0] for r in pre], "ids were renumbered"

    # composite UNIQUE: same code, two DIFFERENT units → both allowed
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('ZMIG1','n',?, 'แผง')", (pid,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('ZMIG1','n',?, 'ตัว')", (pid,))
    conn.commit()
    # same (code, unit) → IntegrityError (composite UNIQUE enforced)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
            "VALUES ('ZMIG1','dup',?, 'แผง')", (pid,))
    conn.rollback()
    conn.close()


# ── upsert_mapping stays backward-compatible (default bsn_unit='') ──────────

def test_upsert_mapping_default_unit_matches_only_blank_row(tmp_db, monkeypatch):
    """upsert_mapping() with no bsn_unit arg (today's every caller) must only
    ever touch the non-split ('') row — never a unit-specific split row."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    pid = conn.execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1"
    ).fetchone()[0]
    # Pre-seed a unit-specific split row for this code.
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code,bsn_name,product_id,bsn_unit) "
        "VALUES ('ZUP1','split',?, 'แผง')", (pid,))
    conn.commit()
    conn.close()

    import config
    import database
    monkeypatch.setattr(config, 'DATABASE_PATH', tmp_db)
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_db)

    models.upsert_mapping('ZUP1', 'generic map', product_id=pid)  # no bsn_unit arg

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = {r['bsn_unit']: r for r in conn.execute(
        "SELECT bsn_unit, bsn_name, product_id FROM product_code_mapping "
        "WHERE bsn_code='ZUP1'"
    )}
    conn.close()

    # the split row survives untouched...
    assert rows['แผง']['bsn_name'] == 'split'
    # ...and a NEW blank catch-all row was created (not merged into 'แผง')
    assert rows['']['bsn_name'] == 'generic map'
