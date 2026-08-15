"""Migration 158 — conversion_formula_inputs.role + one active [แพ็ค] per output.

See docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5 Phase 0. Schema only:
adds a nullable `role` label to conversion_formula_inputs (so a multi-input
formula can say which row is the real component vs. the packaging, without
relying on row order — verified no reader orders by anything meaningful and
no index existed on this table before this migration) and a partial unique
index enforcing "at most one ACTIVE [แพ็ค] formula per output product"
(get_buildable sums over every active formula for an output, so two active
packs would double-count "แปลงได้ตอนนี้").

Fixture `pre158_conn`: same shape as test_mig134's `pre134_conn` — empty_db
clones the LIVE local schema, which does not have 158 yet on this machine,
but might on another. Detect and roll back first so the fixture works either
way (mirrors test_mig157's `pre157_conn` detection).
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_158 = os.path.join(REPO, "data", "migrations", "158_conversion_input_role.sql")
ROLLBACK_158 = os.path.join(
    REPO, "data", "migrations", "158_conversion_input_role.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre158_conn(empty_db_conn):
    cols = {r["name"] for r in
            empty_db_conn.execute("PRAGMA table_info(conversion_formula_inputs)")}
    if "role" in cols:
        _apply(empty_db_conn, ROLLBACK_158)
    return empty_db_conn


def _seed_products(conn, ids):
    conn.executemany(
        "INSERT INTO products (id, product_name) VALUES (?, ?)",
        [(pid, f"test product {pid}") for pid in ids],
    )
    conn.commit()


def _seed_formula(conn, name, output_pid, is_active=1):
    cur = conn.execute(
        "INSERT INTO conversion_formulas (name, output_product_id, is_active) "
        "VALUES (?, ?, ?)",
        (name, output_pid, is_active),
    )
    conn.commit()
    return cur.lastrowid


# ── forward migration ────────────────────────────────────────────────────────

def test_forward_adds_role_column(pre158_conn):
    cols_before = {r["name"] for r in
                   pre158_conn.execute("PRAGMA table_info(conversion_formula_inputs)")}
    assert "role" not in cols_before

    _apply(pre158_conn, MIG_158)

    cols_after = {r["name"] for r in
                  pre158_conn.execute("PRAGMA table_info(conversion_formula_inputs)")}
    assert "role" in cols_after


def test_forward_check_rejects_bogus_role(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [900])
    formula_id = _seed_formula(pre158_conn, "[แพ็ค] test", 900)
    with pytest.raises(sqlite3.IntegrityError):
        pre158_conn.execute(
            "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
            "VALUES (?, 901, 1, 'banana')",
            (formula_id,),
        )


def test_forward_check_accepts_null_and_valid_roles(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [910, 911, 912])
    formula_id = _seed_formula(pre158_conn, "[แพ็ค] test2", 910)
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (?, 911, 1, NULL)", (formula_id,))
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (?, 912, 1, 'component')", (formula_id,))
    pre158_conn.commit()
    rows = pre158_conn.execute(
        "SELECT role FROM conversion_formula_inputs WHERE formula_id=? ORDER BY product_id",
        (formula_id,),
    ).fetchall()
    assert len(rows) == 2
    assert [r["role"] for r in rows] == [None, "component"]


def test_forward_creates_unique_index(pre158_conn):
    _apply(pre158_conn, MIG_158)
    idx = pre158_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_conv_active_pack_per_output'"
    ).fetchone()
    assert idx is not None
    assert "UNIQUE" in idx["sql"].upper()


def test_forward_existing_single_input_shape_untouched(pre158_conn):
    """122 pre-existing formulas are all single-input with role NULL — the
    migration must not backfill anything into them."""
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [920, 921, 922, 923])
    f1 = _seed_formula(pre158_conn, "[แพ็ค] legacy1", 920)
    f2 = _seed_formula(pre158_conn, "[แกะ] legacy2", 922)
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity) "
        "VALUES (?, 921, 1)", (f1,))
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity) "
        "VALUES (?, 923, 1)", (f2,))
    pre158_conn.commit()
    rows = pre158_conn.execute(
        "SELECT role FROM conversion_formula_inputs WHERE formula_id IN (?, ?)",
        (f1, f2),
    ).fetchall()
    assert len(rows) == 2
    assert all(r["role"] is None for r in rows)


# ── the unique index invariant ───────────────────────────────────────────────

def test_index_rejects_second_active_pack_for_same_output(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [930])
    _seed_formula(pre158_conn, "[แพ็ค] first", 930, is_active=1)
    with pytest.raises(sqlite3.IntegrityError):
        pre158_conn.execute(
            "INSERT INTO conversion_formulas (name, output_product_id, is_active) "
            "VALUES ('[แพ็ค] second', 930, 1)"
        )


def test_index_allows_second_pack_when_first_inactive(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [931])
    _seed_formula(pre158_conn, "[แพ็ค] first", 931, is_active=0)
    fid = _seed_formula(pre158_conn, "[แพ็ค] second", 931, is_active=1)
    rows = pre158_conn.execute(
        "SELECT id FROM conversion_formulas WHERE output_product_id=931 AND is_active=1"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == fid


def test_index_allows_two_active_unpack_halves(pre158_conn):
    """6 outputs on prod today have 2 active formulas — all [แกะ] halves.
    The index must not touch that shape (it only matches [แพ็ค] names)."""
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [932])
    _seed_formula(pre158_conn, "[แกะ] pack A", 932, is_active=1)
    _seed_formula(pre158_conn, "[แกะ] pack B", 932, is_active=1)
    rows = pre158_conn.execute(
        "SELECT id FROM conversion_formulas WHERE output_product_id=932 AND is_active=1"
    ).fetchall()
    assert len(rows) == 2


def test_index_break_it_once(pre158_conn):
    """A guard test that has never gone red is not a test: drop the index,
    show the duplicate insert now succeeds, restore it."""
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [933])
    _seed_formula(pre158_conn, "[แพ็ค] first", 933, is_active=1)

    pre158_conn.execute("DROP INDEX ux_conv_active_pack_per_output")
    pre158_conn.execute(
        "INSERT INTO conversion_formulas (name, output_product_id, is_active) "
        "VALUES ('[แพ็ค] second', 933, 1)"
    )
    pre158_conn.commit()
    rows = pre158_conn.execute(
        "SELECT id FROM conversion_formulas WHERE output_product_id=933 AND is_active=1"
    ).fetchall()
    assert len(rows) == 2, "guard removed: duplicate active [แพ็ค] silently succeeded"

    pre158_conn.execute(
        "CREATE UNIQUE INDEX ux_conv_active_pack_per_output "
        "ON conversion_formulas(output_product_id) "
        "WHERE is_active = 1 AND name LIKE '[แพ็ค]%'"
    )
    pre158_conn.commit()


# ── rollback ──────────────────────────────────────────────────────────────

def test_rollback_removes_role_column_and_index(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _apply(pre158_conn, ROLLBACK_158)
    cols = {r["name"] for r in
            pre158_conn.execute("PRAGMA table_info(conversion_formula_inputs)")}
    assert "role" not in cols
    idx = pre158_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='ux_conv_active_pack_per_output'"
    ).fetchone()
    assert idx is None


def test_rollback_preserves_data_including_rows_inserted_after_forward(pre158_conn):
    """Rollback must restore a logically identical schema/dataset — comparing
    row VALUES, not COUNT(*) — and must preserve rows inserted AFTER the
    forward migration ran (never a snapshot)."""
    _apply(pre158_conn, MIG_158)
    _seed_products(pre158_conn, [940, 941, 942])
    f1 = _seed_formula(pre158_conn, "[แพ็ค] preexisting", 940)
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (?, 941, 1, 'component')", (f1,))
    pre158_conn.commit()

    # A row inserted AFTER the forward migration (post-forward activity).
    f2 = _seed_formula(pre158_conn, "[แกะ] added later", 942)
    pre158_conn.execute(
        "INSERT INTO conversion_formula_inputs (formula_id, product_id, quantity, role) "
        "VALUES (?, 940, 1, NULL)", (f2,))
    pre158_conn.commit()

    before = sorted(
        (r["formula_id"], r["product_id"], r["quantity"])
        for r in pre158_conn.execute(
            "SELECT formula_id, product_id, quantity FROM conversion_formula_inputs "
            "WHERE formula_id IN (?, ?)", (f1, f2))
    )
    assert len(before) == 2

    _apply(pre158_conn, ROLLBACK_158)

    after = sorted(
        (r["formula_id"], r["product_id"], r["quantity"])
        for r in pre158_conn.execute(
            "SELECT formula_id, product_id, quantity FROM conversion_formula_inputs "
            "WHERE formula_id IN (?, ?)", (f1, f2))
    )
    assert after == before


def test_forward_rollback_forward_is_clean(pre158_conn):
    _apply(pre158_conn, MIG_158)
    _apply(pre158_conn, ROLLBACK_158)
    _apply(pre158_conn, MIG_158)  # must not raise "duplicate column" / "index exists"
    cols = {r["name"] for r in
            pre158_conn.execute("PRAGMA table_info(conversion_formula_inputs)")}
    assert "role" in cols


def test_rollback_schema_logically_identical_to_pre_forward(pre158_conn):
    """Compare sqlite_master.sql text of the affected objects before the
    forward migration vs. after forward+rollback (SQLite gives no
    byte-for-byte file guarantee, so this is the right level to compare)."""
    before_table = pre158_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversion_formula_inputs'"
    ).fetchone()["sql"]

    _apply(pre158_conn, MIG_158)
    _apply(pre158_conn, ROLLBACK_158)

    after_table = pre158_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversion_formula_inputs'"
    ).fetchone()["sql"]
    assert after_table == before_table

    idx = pre158_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ux_conv_active_pack_per_output'"
    ).fetchone()
    assert idx is None
