"""upsert_pack_unpack_pair — create/update a pack↔loose pair (both formulas at
once), idempotently. Powers the /conversions pair-mode form.

PACK   : output=pack qty1, input=[(loose, ratio)]
UNPACK : output=loose qty ratio, input=[(pack, 1)]
Dedup key = (output_product_id, frozenset(input_product_ids)) — re-running
updates the matching formula instead of duplicating.
"""
import models


def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute("INSERT INTO products (id, product_name, unit_type) VALUES (?, ?, ?)", (pid, name, unit))


def _active_formulas(conn):
    return conn.execute("SELECT id, output_product_id, output_qty FROM conversion_formulas WHERE is_active=1").fetchall()


def _inputs(conn, fid):
    return [(r["product_id"], r["quantity"]) for r in
            conn.execute("SELECT product_id, quantity FROM conversion_formula_inputs WHERE formula_id=?",
                         (fid,)).fetchall()]


def test_both_directions_creates_two(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    res = models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c)
    c.commit()
    assert res["created"] == 2 and res["updated"] == 0
    fs = {(f["output_product_id"], f["output_qty"]): f["id"] for f in _active_formulas(c)}
    assert (10, 1) in fs and (20, 2) in fs                 # pack out10 qty1, unpack out20 qty2
    assert list(_inputs(c, fs[(10, 1)])) == [(20, 2)]       # pack input = loose × ratio
    assert list(_inputs(c, fs[(20, 2)])) == [(10, 1)]       # unpack input = pack × 1


def test_unpack_only(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    res = models.upsert_pack_unpack_pair(10, 20, ratio=3, direction="unpack", conn=c)
    c.commit()
    assert res["created"] == 1
    fs = _active_formulas(c)
    assert len(fs) == 1 and fs[0]["output_product_id"] == 20 and fs[0]["output_qty"] == 3


def test_pack_only(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    res = models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="pack", conn=c)
    c.commit()
    assert res["created"] == 1
    fs = _active_formulas(c)
    assert len(fs) == 1 and fs[0]["output_product_id"] == 10


def test_idempotent_rerun_updates_not_duplicates(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c); c.commit()
    res2 = models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c); c.commit()
    assert res2["created"] == 0 and res2["updated"] == 2
    assert len(_active_formulas(c)) == 2                    # still 2, no duplicates


def test_ratio_change_updates_existing(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c); c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=4, direction="both", conn=c); c.commit()
    assert len(_active_formulas(c)) == 2
    fs = {f["output_product_id"]: f for f in _active_formulas(c)}
    assert fs[20]["output_qty"] == 4                        # unpack qty updated 2 → 4
    assert list(_inputs(c, fs[10]["id"])) == [(20, 4)]      # pack input qty updated 2 → 4


# ── find_pair_partner — reciprocal partner lookup that keeps deletes from ──────
# orphaning the other half of a [แพ็ค]/[แกะ] pair (see models.find_pair_partner).

def _mk_formula(conn, name, output_pid, output_qty, inputs):
    """Insert a raw formula + its inputs on `conn`; return the new formula id."""
    fid = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        (name, output_pid, output_qty)).lastrowid
    for pid, qty in inputs:
        conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
                     (fid, pid, qty))
    return fid


def test_find_partner_happy_path(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c); c.commit()
    fs = {f["output_product_id"]: f["id"] for f in _active_formulas(c)}
    pack_fid, unpack_fid = fs[10], fs[20]
    p = models.find_pair_partner(pack_fid, conn=c)
    assert p is not None and p["id"] == unpack_fid          # pack → its unpack
    p2 = models.find_pair_partner(unpack_fid, conn=c)
    assert p2 is not None and p2["id"] == pack_fid          # symmetric


def test_find_partner_shared_loose_disambiguates(empty_db_conn):
    # Real #3043 shape: ONE loose (2015) is the ตัว for TWO packs (93 & 94), so
    # two [แกะ] formulas share output=2015. Matching on output alone over-matches;
    # the full reciprocal must pick the partner whose input is THIS pack.
    c = empty_db_conn
    _seed_product(c, 2015, "loose", "ตัว")
    _seed_product(c, 93, "pack แผง1", "แผง"); _seed_product(c, 94, "pack แผง2", "แผง")
    c.commit()
    models.upsert_pack_unpack_pair(93, 2015, ratio=2, direction="both", conn=c); c.commit()
    models.upsert_pack_unpack_pair(94, 2015, ratio=2, direction="both", conn=c); c.commit()
    pack_93 = c.execute("SELECT id FROM conversion_formulas WHERE output_product_id=93 AND is_active=1").fetchone()["id"]
    p = models.find_pair_partner(pack_93, conn=c)
    assert p is not None
    assert p["output_product_id"] == 2015
    assert list(_inputs(c, p["id"])) == [(93, 1)]           # the unpack for #93, NOT #94


def test_find_partner_multi_input_returns_none(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 50, "out", "ตัว"); _seed_product(c, 51, "a", "ตัว"); _seed_product(c, 52, "b", "ตัว")
    fid = _mk_formula(c, "general mix", 50, 1, [(51, 1), (52, 1)])
    c.commit()
    assert models.find_pair_partner(fid, conn=c) is None    # >1 input → not a pair-half


def test_find_partner_inactive_not_matched(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="both", conn=c); c.commit()
    fs = {f["output_product_id"]: f["id"] for f in _active_formulas(c)}
    c.execute("UPDATE conversion_formulas SET is_active=0 WHERE id=?", (fs[20],)); c.commit()
    assert models.find_pair_partner(fs[10], conn=c) is None  # deactivated partner not matched


def test_find_partner_no_partner_returns_none(empty_db_conn):
    c = empty_db_conn
    _seed_product(c, 10, "pack", "แผง"); _seed_product(c, 20, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(10, 20, ratio=2, direction="pack", conn=c); c.commit()
    fs = _active_formulas(c)
    assert len(fs) == 1
    # one-way pack-only formula → no partner (this is the signal the list flags as "ทิศเดียว")
    assert models.find_pair_partner(fs[0]["id"], conn=c) is None


def test_find_partner_ignores_non_prefixed_reciprocal(empty_db_conn):
    # Two GENERIC reciprocal single-input formulas (no [แพ็ค]/[แกะ] prefix) are NOT
    # a pack/unpack pair — must not be offered "delete both".
    c = empty_db_conn
    _seed_product(c, 60, "A", "ตัว"); _seed_product(c, 61, "B", "ตัว")
    f1 = _mk_formula(c, "A→B generic", 61, 1, [(60, 1)])
    f2 = _mk_formula(c, "B→A generic", 60, 1, [(61, 1)])
    c.commit()
    assert models.find_pair_partner(f1, conn=c) is None
    assert models.find_pair_partner(f2, conn=c) is None
