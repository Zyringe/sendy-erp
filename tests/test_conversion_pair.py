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
