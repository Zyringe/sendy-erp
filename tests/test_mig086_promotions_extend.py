"""Migration 086 — promotions: extend for bundle / gift / mixed promo types.

The migration:
  - relaxes promo_type CHECK to allow 'bundle', 'mixed', 'gift'
  - adds 7 columns: bundle_buy, bundle_free, bundle_unit, bundle_condition,
    bundle_tiers_json, gift_desc, gift_qty
  - makes discount_value nullable
  - adds 2 indexes (product_id, is_active+product_id)
  - adds 3 audit triggers (INSERT/UPDATE/DELETE → audit_log)
  - changes product_id FK to ON DELETE CASCADE
  - enforces strict per-type shape via CHECK (bundle MUST have bundle_buy etc.)

Tests verify on a tmp_db copy of live:
  1. Schema shape after mig: 7 new cols + 2 indexes + 3 triggers + nullable
     discount_value + cascade on product_id.
  2. Pre-existing promotion rows preserved (currently 0 in prod; the
     INSERT…SELECT is column-explicit and must not lose data).
  3. CHECK constraint accepts well-formed inserts (each of 5 promo_type values).
  4. CHECK rejects malformed inserts: bundle without bundle_buy; percent with
     bad discount_value; gift without gift_desc; bad bundle_condition.
  5. Audit triggers write to audit_log on INSERT/UPDATE/DELETE.
  6. ON DELETE CASCADE: deleting a product wipes its promotions.
  7. Rollback aborts cleanly when extended-type rows exist (data-safety).
  8. Rollback succeeds when no extended-type rows exist; schema reverts.
"""
import json
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_086 = os.path.join(REPO, "data", "migrations",
                      "086_promotions_extend_for_bundle_gift.sql")
ROLLBACK_086 = os.path.join(REPO, "data", "migrations",
                            "086_promotions_extend_for_bundle_gift.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _apply_086(conn):
    """Apply mig 086 to a connection. Snapshot may or may not have it applied;
    handle both via filename check in applied_migrations."""
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "086_promotions_extend_for_bundle_gift.sql" not in applied:
        _apply(conn, MIG_086)


def _cols(conn):
    return {r[1]: r for r in conn.execute("PRAGMA table_info(promotions)")}


def _indexes(conn):
    return {r[1] for r in conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='promotions'")}


def _triggers(conn):
    return {r[1] for r in conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name='promotions'")}


def _pick_real_product_id(conn):
    """Find a real product to attach promotions to (FK requires it)."""
    row = conn.execute("SELECT id FROM products LIMIT 1").fetchone()
    assert row is not None, "live DB has no products — test cannot run"
    return row[0]


# ── 1. Schema shape ─────────────────────────────────────────────────────────

def test_new_columns_present(tmp_db):
    """7 new columns exist with correct nullability."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    cols = _cols(conn)
    for c in ("bundle_buy", "bundle_free", "bundle_unit", "bundle_condition",
              "bundle_tiers_json", "gift_desc", "gift_qty"):
        assert c in cols, f"column {c} missing after mig 086"
        # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
        assert cols[c][3] == 0, f"{c} should be nullable"
    conn.close()


def test_discount_value_now_nullable(tmp_db):
    """discount_value was NOT NULL pre-086; now nullable."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    cols = _cols(conn)
    assert cols["discount_value"][3] == 0, "discount_value should be nullable"
    conn.close()


def test_indexes_created(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    idx = _indexes(conn)
    assert "idx_promotions_product" in idx
    assert "idx_promotions_active" in idx
    conn.close()


def test_audit_triggers_created(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    triggers = _triggers(conn)
    assert "audit_promotions_insert" in triggers
    assert "audit_promotions_update" in triggers
    assert "audit_promotions_delete" in triggers
    conn.close()


def test_product_id_has_on_delete_cascade(tmp_db):
    """product_id FK should now have ON DELETE CASCADE."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    fks = list(conn.execute("PRAGMA foreign_key_list(promotions)"))
    # row: (id, seq, table, from, to, on_update, on_delete, match)
    pid_fk = [fk for fk in fks if fk[3] == "product_id"]
    assert pid_fk, "product_id FK missing"
    assert pid_fk[0][6] == "CASCADE", \
        f"product_id should be ON DELETE CASCADE, got {pid_fk[0][6]!r}"
    conn.close()


# ── 2. Data preservation ─────────────────────────────────────────────────────

def test_existing_rows_preserved(tmp_db):
    """Any pre-existing promotion rows must survive the rebuild."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    # Snapshot before
    before = conn.execute("SELECT id, product_id, promo_type, discount_value "
                          "FROM promotions ORDER BY id").fetchall()

    _apply_086(conn)

    # Snapshot after — for the same columns
    after = conn.execute("SELECT id, product_id, promo_type, discount_value "
                         "FROM promotions ORDER BY id").fetchall()
    assert before == after, "data lost during rebuild"
    conn.close()


# ── 3. CHECK accepts well-formed rows (one per type) ─────────────────────────

def test_check_accepts_percent(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
        "VALUES (?, 'test_pct', 'percent', 10)", (pid,))
    conn.commit()
    conn.close()


def test_check_accepts_fixed(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
        "VALUES (?, 'test_fixed', 'fixed', 199.50)", (pid,))
    conn.commit()
    conn.close()


def test_check_accepts_bundle(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        bundle_buy, bundle_free, bundle_unit) "
        "VALUES (?, 'test_bundle', 'bundle', 12, 1, 'ดอก')", (pid,))
    conn.commit()
    conn.close()


def test_check_accepts_bundle_with_tiers_json(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    tiers = json.dumps([{"buy": 12, "free": 1}, {"buy": 24, "free": 3}])
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        bundle_buy, bundle_free, bundle_tiers_json) "
        "VALUES (?, 'test_tier', 'bundle', 12, 1, ?)", (pid, tiers))
    conn.commit()
    conn.close()


def test_check_accepts_gift(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        gift_desc, gift_qty) "
        "VALUES (?, 'test_gift', 'gift', 'ดจ.สแตนเลส', '20 ดอก')", (pid,))
    conn.commit()
    conn.close()


def test_check_accepts_mixed(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        discount_value, bundle_buy, bundle_free, bundle_unit) "
        "VALUES (?, 'test_mixed', 'mixed', 10, 120, 12, 'ดอก')", (pid,))
    conn.commit()
    conn.close()


def test_check_accepts_percent_with_bundle_condition(tmp_db):
    """`ซื้อยกลัง ลด 5%` shape: promo_type='percent' + bundle_condition='ยกลัง'."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        discount_value, bundle_condition) "
        "VALUES (?, 'test_yk', 'percent', 5, 'ยกลัง')", (pid,))
    conn.commit()
    conn.close()


# ── 4. CHECK rejects malformed rows ─────────────────────────────────────────

def test_check_rejects_bundle_without_buy(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, bundle_free) "
            "VALUES (?, 'bad', 'bundle', 1)", (pid,))
        conn.commit()
    conn.close()


def test_check_rejects_percent_over_100(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
            "VALUES (?, 'bad_pct', 'percent', 150)", (pid,))
        conn.commit()
    conn.close()


def test_check_rejects_gift_without_desc(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, gift_qty) "
            "VALUES (?, 'bad_gift', 'gift', '20 ดอก')", (pid,))
        conn.commit()
    conn.close()


def test_check_rejects_unknown_bundle_condition(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, "
            "                        discount_value, bundle_condition) "
            "VALUES (?, 'bad_cond', 'percent', 10, 'foobar')", (pid,))
        conn.commit()
    conn.close()


def test_check_rejects_invalid_type(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
            "VALUES (?, 'bad_type', 'tradein', 10)", (pid,))
        conn.commit()
    conn.close()


def test_check_rejects_percent_with_bundle_fields(tmp_db):
    """Percent type must not have bundle/gift fields populated."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO promotions (product_id, promo_name, promo_type, "
            "                        discount_value, bundle_buy) "
            "VALUES (?, 'bad_pct_bb', 'percent', 10, 12)", (pid,))
        conn.commit()
    conn.close()


# ── 5. Audit triggers ────────────────────────────────────────────────────────

def test_audit_log_on_insert_update_delete(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)

    def audit_count(action):
        return conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE table_name='promotions' AND action=?", (action,)
        ).fetchone()[0]

    before_ins = audit_count("INSERT")
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
        "VALUES (?, 'audit_test', 'percent', 10)", (pid,))
    new_id = cur.lastrowid
    conn.commit()
    assert audit_count("INSERT") == before_ins + 1

    before_upd = audit_count("UPDATE")
    conn.execute("UPDATE promotions SET discount_value = 15 WHERE id = ?", (new_id,))
    conn.commit()
    assert audit_count("UPDATE") == before_upd + 1

    before_del = audit_count("DELETE")
    conn.execute("DELETE FROM promotions WHERE id = ?", (new_id,))
    conn.commit()
    assert audit_count("DELETE") == before_del + 1
    conn.close()


def test_audit_update_skips_noop(tmp_db):
    """WHEN clause should suppress audit row for no-change UPDATE."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)
    cur = conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
        "VALUES (?, 'noop_test', 'percent', 10)", (pid,))
    new_id = cur.lastrowid
    conn.commit()
    before_upd = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='promotions' AND action='UPDATE' AND row_id=?",
        (new_id,)).fetchone()[0]
    # UPDATE that doesn't change any tracked field
    conn.execute("UPDATE promotions SET discount_value = 10 WHERE id = ?", (new_id,))
    conn.commit()
    after_upd = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE table_name='promotions' AND action='UPDATE' AND row_id=?",
        (new_id,)).fetchone()[0]
    assert after_upd == before_upd, "no-op UPDATE should NOT emit audit row"
    conn.close()


# ── 6. ON DELETE CASCADE ─────────────────────────────────────────────────────

def test_on_delete_cascade(tmp_db):
    """Deleting a product cascades to its promotions."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    # Insert a throwaway product for the cascade test.
    cur = conn.execute(
        "INSERT INTO products (sku, product_name) VALUES (999998, 'cascade_test')")
    new_pid = cur.lastrowid
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value) "
        "VALUES (?, 'cascade_promo', 'percent', 10)", (new_pid,))
    conn.commit()

    promo_count_before = conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE product_id = ?", (new_pid,)
    ).fetchone()[0]
    assert promo_count_before == 1

    conn.execute("DELETE FROM products WHERE id = ?", (new_pid,))
    conn.commit()

    promo_count_after = conn.execute(
        "SELECT COUNT(*) FROM promotions WHERE product_id = ?", (new_pid,)
    ).fetchone()[0]
    assert promo_count_after == 0, "promotions did not cascade-delete with product"
    conn.close()


# ── 7+8. Rollback safety + happy path ────────────────────────────────────────

def test_rollback_aborts_with_extended_type_rows(tmp_db):
    """Rollback must REFUSE if any bundle/mixed/gift rows exist."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)
    pid = _pick_real_product_id(conn)

    # Insert one extended-type row.
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, "
        "                        bundle_buy, bundle_free) "
        "VALUES (?, 'extended', 'bundle', 12, 1)", (pid,))
    conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        _apply(conn, ROLLBACK_086)

    # Schema must still be the post-086 shape after the aborted rollback.
    cols = _cols(conn)
    assert "bundle_buy" in cols, "rollback should have aborted; bundle_buy still present"
    conn.close()


def test_rollback_succeeds_when_clean(tmp_db):
    """Rollback works when no extended-type rows exist."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_086(conn)

    # Wipe any percent/fixed rows that may have been added by prior tests
    # in the live DB snapshot; rollback only requires no bundle/mixed/gift
    # rows, but we don't need to test that — the abort guard already covered it.
    conn.execute(
        "DELETE FROM promotions WHERE promo_type IN ('bundle','mixed','gift')")
    conn.commit()

    _apply(conn, ROLLBACK_086)

    # After rollback, the 7 new columns should be gone.
    cols = _cols(conn)
    for c in ("bundle_buy", "bundle_free", "bundle_unit", "bundle_condition",
              "bundle_tiers_json", "gift_desc", "gift_qty"):
        assert c not in cols, f"column {c} should be dropped by rollback"

    # The 2 indexes should be gone.
    idx = _indexes(conn)
    assert "idx_promotions_product" not in idx
    assert "idx_promotions_active" not in idx

    # The 3 audit triggers should be gone.
    triggers = _triggers(conn)
    assert "audit_promotions_insert" not in triggers
    assert "audit_promotions_update" not in triggers
    assert "audit_promotions_delete" not in triggers

    # discount_value back to NOT NULL.
    assert cols["discount_value"][3] == 1, "discount_value should be NOT NULL after rollback"

    # applied_migrations row removed.
    still_applied = conn.execute(
        "SELECT 1 FROM applied_migrations "
        "WHERE filename = '086_promotions_extend_for_bundle_gift.sql'"
    ).fetchone()
    assert still_applied is None, "applied_migrations row should be removed by rollback"
    conn.close()
