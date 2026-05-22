"""Migration 077 — data-quality fixes from tracker XLSX (PR-A scope).

Covers the SAFE half of data_quality_tracker_2026-05-21.xlsx:
  • 3 new 3rd-party brands (FOUR STARS / BEYOND / ALTECO)
  • brand_id set on 98 D5 NoBrand products (28 Sendai / 2 GL / 1 BullTech /
    2 FOUR STARS / 2 BEYOND / 1 ALTECO / 62 Other)
  • unit_type 'กก.' → 'กิโลกรัม' on 17 D3 products

PR-B (D1 SWAP / WRONG_MAP / NEW_PRODUCT_ID / D3 complex + stock recompute)
deferred — see migration header for blockers.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_077 = os.path.join(REPO, "data", "migrations",
                      "077_data_quality_brand_kg_rename.sql")
ROLLBACK_077 = os.path.join(REPO, "data", "migrations",
                           "077_data_quality_brand_kg_rename.rollback.sql")


# Pid lists — kept in sync with the migration's hardcoded WHERE clauses.
SENDAI_PIDS = [
    401, 191, 189, 190, 402, 414, 411, 214, 215, 221, 219, 400, 216, 220, 56,
    410, 403, 53, 218, 52, 59, 54, 62, 211, 60, 217, 418, 212,
]
GL_PIDS = [621, 576]
BULLTECH_PIDS = [1033]
FOUR_STARS_PIDS = [406, 407]
BEYOND_PIDS = [776, 775]
ALTECO_PIDS = [872]
OTHER_PIDS = [
    660, 988, 855, 853, 588, 764, 727, 648, 834, 548, 405, 573, 999, 768,
    1691, 713, 572, 1363, 412, 1003, 698, 763, 704, 729, 1651, 702, 1330,
    587, 659, 575, 1303, 686, 1353, 1347, 1647, 577, 578, 1362, 1646, 586,
    1821, 1517, 1687, 1862, 1709, 549, 1202, 1364, 658, 554, 556, 1176, 557,
    1639, 1200, 1565, 618, 1883, 1358, 1025, 1102, 1195,
]
KG_RENAME_PIDS = [
    414, 415, 416, 472, 473, 474, 475, 476, 477, 681, 682, 683, 684, 685, 686,
    687, 912,
]
ALL_D5_PIDS = (
    SENDAI_PIDS + GL_PIDS + BULLTECH_PIDS + FOUR_STARS_PIDS + BEYOND_PIDS
    + ALTECO_PIDS + OTHER_PIDS
)


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _reset_to_pre_mig(conn):
    """If the live DB clone already has mig 077 applied, run its rollback
    so the test starts from pre-mig state. The rollback restores from
    migration_077_snapshot if it exists, then drops the table; absent the
    snapshot table, this is a no-op."""
    snap = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='migration_077_snapshot'"
    ).fetchone()
    if snap is not None:
        _apply(conn, ROLLBACK_077)
        # Drop the applied_migrations bookkeeping row too so a subsequent
        # in-test _apply(MIG_077) is still treated as a first apply by any
        # idempotency assertions. (Tests bypass the runner, but keep the
        # state coherent.)
        conn.execute(
            "DELETE FROM applied_migrations "
            "WHERE filename='077_data_quality_brand_kg_rename.sql'"
        )
        conn.commit()


def _snapshot_state(conn, pids):
    """Capture {pid: (brand_id, unit_type)} for the given pids."""
    placeholders = ",".join("?" * len(pids))
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            f"SELECT id, brand_id, unit_type FROM products WHERE id IN ({placeholders})",
            pids,
        )
    }


# ── Counts ───────────────────────────────────────────────────────────────────

def test_d5_pid_count_is_98():
    assert len(ALL_D5_PIDS) == 98
    assert len(set(ALL_D5_PIDS)) == 98, "D5 buckets must be disjoint"


def test_kg_rename_pid_count_is_17():
    assert len(KG_RENAME_PIDS) == 17


# ── Brand creation ───────────────────────────────────────────────────────────

def test_mig_077_creates_3_new_brands(tmp_db):
    """After mig 077, three new brand rows exist with the expected codes."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    codes = {r[0] for r in conn.execute(
        "SELECT code FROM brands WHERE code IN ('four_stars','beyond','alteco')"
    )}
    assert codes == {"four_stars", "beyond", "alteco"}

    # is_own_brand is 0 for all three (third-party)
    flags = {r[0] for r in conn.execute(
        "SELECT is_own_brand FROM brands WHERE code IN ('four_stars','beyond','alteco')"
    )}
    assert flags == {0}


def test_mig_077_new_brands_have_short_codes(tmp_db):
    """Codex finding 2026-05-23: brands.short_code must be populated.
    sku_code generation drops the brand segment when short_code is NULL,
    which would corrupt SKU codes of unlocked products assigned to these
    brands by this migration."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    rows = dict(conn.execute(
        "SELECT code, short_code FROM brands "
        "WHERE code IN ('four_stars', 'beyond', 'alteco')"
    ).fetchall())
    assert rows == {
        'four_stars': '4STAR',
        'beyond':     'BYND',
        'alteco':     'ALTECO',
    }

    # short_codes must be unique across all brands
    dupes = list(conn.execute(
        "SELECT short_code, COUNT(*) n FROM brands "
        "WHERE short_code IS NOT NULL "
        "GROUP BY short_code HAVING n > 1"
    ))
    assert dupes == [], f"duplicate brand short_codes: {dupes}"


def test_mig_077_brand_inserts_idempotent(tmp_db):
    """Re-applying mig 077 does not create duplicate brand rows."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)
    before = conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
    _apply(conn, MIG_077)
    after = conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]
    assert before == after


# ── D5 brand assignments ─────────────────────────────────────────────────────

def test_mig_077_assigns_sendai_brand_to_28_pids(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    placeholders = ",".join("?" * len(SENDAI_PIDS))
    rows = conn.execute(
        f"SELECT id, brand_id FROM products WHERE id IN ({placeholders})",
        SENDAI_PIDS,
    ).fetchall()
    assert len(rows) == 28
    for pid, brand_id in rows:
        assert brand_id == 3, f"pid {pid} brand_id={brand_id}, expected 3 (Sendai)"


def test_mig_077_assigns_golden_lion_brand_to_2_pids(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    rows = conn.execute(
        "SELECT id, brand_id FROM products WHERE id IN (621, 576)"
    ).fetchall()
    assert {r[1] for r in rows} == {1}


def test_mig_077_assigns_bulltech_brand_to_pid_1033(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    brand_id = conn.execute(
        "SELECT brand_id FROM products WHERE id = 1033"
    ).fetchone()[0]
    assert brand_id == 52


def test_mig_077_assigns_new_brands_to_5_pids(tmp_db):
    """FOUR STARS (2) + BEYOND (2) + ALTECO (1) get pointed at the new brand rows."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    four_id = conn.execute("SELECT id FROM brands WHERE code='four_stars'").fetchone()[0]
    beyond_id = conn.execute("SELECT id FROM brands WHERE code='beyond'").fetchone()[0]
    alteco_id = conn.execute("SELECT id FROM brands WHERE code='alteco'").fetchone()[0]

    rows = dict(conn.execute(
        "SELECT id, brand_id FROM products WHERE id IN (406, 407, 776, 775, 872)"
    ).fetchall())
    assert rows == {
        406: four_id, 407: four_id,
        776: beyond_id, 775: beyond_id,
        872: alteco_id,
    }


def test_mig_077_assigns_other_brand_to_62_pids(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    placeholders = ",".join("?" * len(OTHER_PIDS))
    rows = conn.execute(
        f"SELECT id, brand_id FROM products WHERE id IN ({placeholders})",
        OTHER_PIDS,
    ).fetchall()
    assert len(rows) == 62
    for pid, brand_id in rows:
        assert brand_id == 13, f"pid {pid} brand_id={brand_id}, expected 13 (Other)"


def test_mig_077_all_98_d5_pids_have_brand_id_after(tmp_db):
    """No D5 pid should be left with NULL brand_id after the migration."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn, MIG_077)

    placeholders = ",".join("?" * len(ALL_D5_PIDS))
    null_pids = [r[0] for r in conn.execute(
        f"SELECT id FROM products WHERE id IN ({placeholders}) AND brand_id IS NULL",
        ALL_D5_PIDS,
    )]
    assert null_pids == [], f"D5 pids still NULL after mig: {null_pids}"


# ── D3 kg-rename ─────────────────────────────────────────────────────────────

def test_mig_077_renames_unit_type_to_kilogram(tmp_db):
    """All 17 D3 pids that started as 'กก.' end as 'กิโลกรัม'."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_to_pre_mig(conn)

    placeholders = ",".join("?" * len(KG_RENAME_PIDS))
    before = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, unit_type FROM products WHERE id IN ({placeholders})",
        KG_RENAME_PIDS,
    )}

    # Premise check: at least one pid must actually be 'กก.' pre-migration,
    # else the loop below asserts nothing and the test passes vacuously
    # (e.g., mig already applied, or someone fixed them manually).
    pre_kg_count = sum(1 for pid in KG_RENAME_PIDS if before.get(pid) == "กก.")
    assert pre_kg_count > 0, (
        "Test premise broken: none of the 17 kg-rename pids start as 'กก.' "
        "in the test DB. The migration's rename UPDATE has nothing to do. "
        f"Current unit_types: {before!r}"
    )

    _apply(conn, MIG_077)

    after = {r[0]: r[1] for r in conn.execute(
        f"SELECT id, unit_type FROM products WHERE id IN ({placeholders})",
        KG_RENAME_PIDS,
    )}

    for pid in KG_RENAME_PIDS:
        if before.get(pid) == "กก.":
            assert after[pid] == "กิโลกรัม", \
                f"pid {pid} expected 'กิโลกรัม', got {after[pid]!r}"


def test_mig_077_kg_rename_does_not_touch_other_unit_types(tmp_db):
    """The 'AND unit_type = กก.' guard prevents accidental flips of other unit_types."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    # Pick a pid NOT in the kg-rename list and confirm its unit_type is unchanged.
    sample_pid, sample_ut = conn.execute(
        "SELECT id, unit_type FROM products WHERE id NOT IN (414,415,416,472,473,474,475,476,477,681,682,683,684,685,686,687,912) "
        "AND unit_type IS NOT NULL LIMIT 1"
    ).fetchone()

    _apply(conn, MIG_077)

    after_ut = conn.execute(
        "SELECT unit_type FROM products WHERE id = ?", (sample_pid,)
    ).fetchone()[0]
    assert after_ut == sample_ut


# ── Snapshot table + rollback ────────────────────────────────────────────────

def test_mig_077_snapshot_captures_pre_state(tmp_db):
    """migration_077_snapshot has a row per affected pid with the PRIOR
    brand_id and unit_type."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_to_pre_mig(conn)

    affected = sorted(set(ALL_D5_PIDS + KG_RENAME_PIDS))
    pre = _snapshot_state(conn, affected)

    _apply(conn, MIG_077)

    snap = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, prior_brand_id, prior_unit_type FROM migration_077_snapshot"
    )}

    assert set(snap.keys()) == set(affected), \
        f"snapshot missing pids: {set(affected) - set(snap.keys())}"

    for pid in affected:
        assert snap[pid] == pre[pid], (
            f"pid {pid}: snapshot={snap[pid]} vs pre-state={pre[pid]}"
        )


def test_mig_077_idempotent_rerun_preserves_snapshot(tmp_db):
    """Re-applying mig 077 must NOT overwrite the snapshot with already-changed values."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    _apply(conn, MIG_077)
    snap1 = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, prior_brand_id, prior_unit_type FROM migration_077_snapshot"
    )}

    _apply(conn, MIG_077)
    snap2 = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, prior_brand_id, prior_unit_type FROM migration_077_snapshot"
    )}

    assert snap1 == snap2, "Second apply altered the prior-state snapshot"


def test_mig_077_rollback_restores_prior_state(tmp_db):
    """Apply mig 077 then rollback → products row state matches pre-migration."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _reset_to_pre_mig(conn)

    affected = sorted(set(ALL_D5_PIDS + KG_RENAME_PIDS))
    pre = _snapshot_state(conn, affected)

    _apply(conn, MIG_077)
    _apply(conn, ROLLBACK_077)

    post = _snapshot_state(conn, affected)

    assert post == pre, (
        f"Rollback did not restore prior state. "
        f"Diffs: {[(p, pre[p], post[p]) for p in affected if pre[p] != post[p]][:5]}"
    )


def test_mig_077_rollback_drops_new_brands(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    _apply(conn, MIG_077)
    _apply(conn, ROLLBACK_077)

    remaining = [r[0] for r in conn.execute(
        "SELECT code FROM brands WHERE code IN ('four_stars','beyond','alteco')"
    )]
    assert remaining == [], f"Rollback left brands behind: {remaining}"


def test_mig_077_rollback_drops_snapshot_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    _apply(conn, MIG_077)
    _apply(conn, ROLLBACK_077)

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_077_snapshot'"
    ).fetchall()
    assert rows == []


def test_mig_077_rollback_skips_brand_delete_when_still_referenced(tmp_db):
    """If another product is manually pointed at a new brand between forward
    and rollback, the NOT EXISTS guard skips that DELETE rather than blowing
    up the whole rollback with a FK error."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    _apply(conn, MIG_077)

    # Simulate Put manually assigning ANOTHER (unrelated) product to four_stars
    # after the forward mig.
    four_stars_id = conn.execute(
        "SELECT id FROM brands WHERE code='four_stars'"
    ).fetchone()[0]
    bystander_pid = conn.execute(
        "SELECT id FROM products WHERE id NOT IN (406, 407) AND brand_id IS NULL LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE products SET brand_id = ? WHERE id = ?",
        (four_stars_id, bystander_pid),
    )
    conn.commit()

    # Rollback should NOT raise (FK error on FOUR STARS DELETE without the
    # NOT EXISTS guard).
    _apply(conn, ROLLBACK_077)

    # four_stars survives (still referenced by bystander_pid). beyond / alteco
    # are deleted normally (no surviving references).
    survived = {r[0] for r in conn.execute(
        "SELECT code FROM brands WHERE code IN ('four_stars','beyond','alteco')"
    )}
    assert survived == {"four_stars"}, (
        f"Expected only four_stars to survive (bystander reference), got: {survived}"
    )
