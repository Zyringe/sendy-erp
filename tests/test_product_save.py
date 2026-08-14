"""Tests for naming_cascade.save_product — the Master Naming workbench single-
product inline edit (Phase 2).

Editing a product's structured columns rebuilds product_name (via name_builder)
under a backup + BEGIN IMMEDIATE + invariant asserts. Engine-style (direct call)
so it's deterministic in the full suite.

⚠ `sku_code` IS DELIBERATELY NOT REGENERATED HERE (issue #383, 2026-08-14).
------------------------------------------------------------------------
It used to be, lock-aware. The two fields are not the same kind of thing:

    product_name  derived display text — rebuilding it IS the workbench's job
    sku_code      a stable EXTERNAL identity key — ERP ↔ photo-library folder
                  names ↔ reshoot batch.json ↔ the Seller SKU typed into TikTok

Because the stored codes and the generator had drifted apart, saving an
unrelated typo silently moved the key for 14 products, 6 of them live TikTok
SKUs. Nothing errored: `plan_all_batches.py` simply stopped resolving them and
dropped them as out-of-stock (the 2026-08-10 incident — 13 stale refs across 4
files, undetected for 4 days).

Rejected alternatives, and why:
  * warn-then-confirm in the save response — save_product COMMITs before the
    response is serialised, so that is a notification, not a guard.
  * lock-by-default (`sku_code_locked=1`) — keeps the wrong model (mutation is
    valid unless a flag stops it), AND `/products/<id>/regen-sku-code` sets
    `sku_code_locked = 0`, so one legitimate rename silently re-arms the hazard.

Note `nc.apply()` (the bulk cascade) already took this position: it snapshots
`_sku_map` and ROLLS BACK with "sku_code changed" if any code moves. This module
was the inconsistent one.

Moving a sku_code is now only ever an explicit act: `/products/<id>/regen-sku-code`.
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


def test_save_rebuilds_name_but_never_touches_sku_code(editable_product, tmp_path):
    """The core of #383. `SEED-1` is nothing like what the generator would
    produce for this product (`SD-#230-4in-CR-PN`), so this fixture has exactly
    the drift that made real saves dangerous."""
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()

    # CONTROL FIRST: the save must actually have done its job, or "sku_code did
    # not change" would pass on a no-op that never reached the code under test.
    assert row["product_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"
    assert res["new_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"

    assert row["sku_code"] == "SEED-1", "a naming save must never move the join key"
    assert res["sku_code"] == "SEED-1"


def test_save_preserves_sku_even_when_the_generated_one_would_differ(editable_product,
                                                                    tmp_path):
    """Break-it-once in test form: prove the generator really would have moved
    this code, so the test above is pinning behaviour rather than describing a
    product whose sku happened to be stable anyway."""
    import sku_code_utils
    path, pid, _ = editable_product
    nc.save_product(path, pid, {"color_code": "CR"}, backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    stored = conn.execute("SELECT sku_code FROM products WHERE id=?",
                          (pid,)).fetchone()["sku_code"]
    _, would_be = sku_code_utils.regenerate_for_product(conn, pid)   # mutates; not committed
    conn.close()

    assert stored == "SEED-1"
    assert would_be == "SD-#230-4in-CR-PN"
    assert would_be != stored, "fixture no longer has drift — this test proves nothing"


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
    # packaging_short still feeds the GENERATOR (and so the explicit regen
    # route); it just no longer reaches sku_code through a naming save.
    assert row["sku_code"] == "SEED-1"


@pytest.mark.parametrize("locked", [0, 1])
def test_sku_is_preserved_regardless_of_lock_state(editable_product, tmp_path, locked):
    """`sku_code_locked` must stop being what decides this.

    Depending on the flag was the trap in the rejected "lock by default" option:
    `/products/<id>/regen-sku-code` sets `sku_code_locked = 0`, so one legitimate
    rename would silently re-arm the hazard for every later naming save. Both
    parameters must land on the same answer."""
    path, pid, _ = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET sku_code_locked=? WHERE id=?", (locked, pid))
    conn.commit()
    conn.close()

    nc.save_product(path, pid, {"color_code": "CR"}, backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    assert row["product_name"] == "กลอน Sendai #230-4in สีโครเมียม (CR) (แผง)"  # control
    assert row["sku_code"] == "SEED-1"


def test_save_missing_product_raises(empty_db, tmp_path):
    with pytest.raises(nc.ProductNotFound):
        nc.save_product(empty_db, 999999, {"color_code": "CR"},
                        backup_dir=str(tmp_path / "b"))


# ── The explicit path must stay easy, or the fix has just moved the problem ──

@pytest.fixture
def admin_client(tmp_db):
    """Authed admin test client. Session injection, NOT a real login — this
    machine's Python has no hashlib.scrypt (see tests/test_nonstock_precedence.py:35)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def test_the_explicit_regen_route_still_moves_the_sku(admin_client, tmp_db):
    """#383 removes the IMPLICIT mutation only. Deliberately renaming a code is
    a legitimate operation and must remain one click — otherwise the 14 drifted
    products could never be migrated at all.

    Forces its own product: tmp_db clones the LIVE dev DB with its data, so
    picking a real drifting pid would inherit state this test does not control.
    """
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    bid = conn.execute("SELECT id FROM brands WHERE short_code='SD'").fetchone()
    bid = bid[0] if bid else conn.execute(
        "INSERT INTO brands(code,name,name_th,short_code)"
        " VALUES ('z383','Z383','Z383','ZZ')").lastrowid
    conn.execute("DELETE FROM products WHERE sku_code='ZZZ-383-SEED'")
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size,"
        "                     packaging_th, packaging_short, sku_code, is_active)"
        " VALUES ('ทดสอบ 383', ?, 'กลอน', '#383', '4in', 'แผง', 'PN',"
        "         'ZZZ-383-SEED', 1)", (bid,)).lastrowid
    conn.commit()
    conn.close()

    resp = admin_client.post(f'/products/{pid}/regen-sku-code')
    assert resp.status_code in (302, 303), resp.status_code

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT sku_code, sku_code_locked FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    assert row["sku_code"] != "ZZZ-383-SEED", \
        "the explicit route must still regenerate — #383 only removes the silent path"
    assert row["sku_code_locked"] == 0


def test_the_workbench_save_ROUTE_does_not_move_the_sku(admin_client, tmp_db):
    """Route level, not just the engine. `save_product` is only reachable through
    this endpoint, and a route can diverge from the function it wraps (it splats
    the return dict straight into JSON)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM products WHERE sku_code='ZZZ-383-ROUTE'")
    bid = conn.execute("SELECT id FROM brands WHERE short_code='SD'").fetchone()[0]
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size,"
        "                     packaging_th, packaging_short, sku_code, is_active)"
        " VALUES ('ทดสอบ 383 route', ?, 'กลอน', '#383', '4in', 'แผง', 'PN',"
        "         'ZZZ-383-ROUTE', 1)", (bid,)).lastrowid
    conn.commit()
    conn.close()

    resp = admin_client.post(f'/naming/product/{pid}/save', json={"size": "6in"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["ok"] is True, body

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    # CONTROL: the edit really landed, so the sku assertion is not on a no-op.
    assert "6in" in row["product_name"], row["product_name"]
    assert row["sku_code"] == "ZZZ-383-ROUTE", "the route must not move the join key"
    assert body["sku_code"] == "ZZZ-383-ROUTE"
