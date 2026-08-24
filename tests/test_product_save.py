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
        # The name must ROUND-TRIP through name_builder: a free-text name would trip
        # the 2026-08-24 name-loss guard and this test would stop exercising the thing
        # it is named after (sku_code stability). The guard has its own tests below.
        " VALUES ('กลอน Sendai #383-4in (แผง)', ?, 'กลอน', '#383', '4in', 'แผง', 'PN',"
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


# ── name-loss guard (2026-08-24) ─────────────────────────────────────────────
# save_product UPDATEs product_name to the regenerated value unconditionally. If
# the stored name carries text that NO structured column holds, that text is
# destroyed. Measured on the 2026-08-24 prod snapshot: 356 of 2,044 active
# products would lose text this way, 101 of them in stock.
#
# The guard flags only PRE-EXISTING drift: the baseline name is rebuilt BEFORE
# the caller's updates are applied, so a shrink the user causes themselves (e.g.
# clearing `condition`) is still allowed. `allow_name_loss=True` is the explicit
# override — refusing forever would strand the 317 products whose names carry a
# brand or word the columns cannot express.

@pytest.fixture
def orphan_token_product(editable_product):
    """Same product, but its stored name carries 'TAYITA' — a word no column
    holds. Rebuilding drops it. This is the real prod shape (pid 852)."""
    path, pid, bid = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET product_name=? WHERE id=?",
                 ("กลอน TAYITA Sendai #230-4in สีรมดำ (AC) (แผง)", pid))
    conn.commit()
    conn.close()
    return path, pid, bid


def _name(path, pid):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row[0]


def test_save_refuses_when_the_rebuild_would_destroy_stored_text(
        orphan_token_product, tmp_path):
    path, pid, _ = orphan_token_product
    before = _name(path, pid)

    with pytest.raises(nc.NameLossRefused) as e:
        nc.save_product(path, pid, {"color_code": "CR"},
                        backup_dir=str(tmp_path / "b"))

    # the message must NAME what would be lost, or the operator cannot act on it
    assert "TAYITA" in str(e.value), str(e.value)
    # ...and carry BOTH names. A clean suffix drop reads fine as a bare fragment, but a
    # mid-word disagreement does not — prod pid 665's misspelled sub_category renders as
    # the fragment 'นก', which is true and useless. The before/after pair is what makes
    # every case actionable, so it is part of the contract, not decoration.
    assert e.value.old_name == before
    assert "TAYITA" not in e.value.new_name and e.value.new_name
    assert "เดิม:" in str(e.value) and "ใหม่:" in str(e.value)
    # nothing was written: neither the name nor the field the caller submitted
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT product_name, color_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    assert row[0] == before
    assert row[1] == "AC", "the refused save must not have applied color_code"

    # CONTROL — the identical call on a product with NO orphan token must SUCCEED,
    # so a guard that simply refused everything could not pass this test. It has to
    # be a SEPARATE row: `orphan_token_product` mutates `editable_product` in place,
    # so asking for both fixtures hands back the same product twice.
    conn = sqlite3.connect(path)
    bid = conn.execute("SELECT brand_id FROM products WHERE id=?", (pid,)).fetchone()[0]
    ctl = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size, "
        "                     color_code, packaging_th, packaging_short, sku_code) "
        "VALUES ('กลอน Sendai #231-4in สีรมดำ (AC) (แผง)', ?, 'กลอน', '#231', '4in', "
        "        'AC', 'แผง', 'PN', 'SEED-CTL')", (bid,)).lastrowid
    conn.commit()
    conn.close()
    nc.save_product(path, ctl, {"color_code": "CR"}, backup_dir=str(tmp_path / "c"))
    assert "สีโครเมียม" in _name(path, ctl)


def test_allow_name_loss_lets_the_operator_through_on_purpose(
        orphan_token_product, tmp_path):
    path, pid, _ = orphan_token_product
    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"), allow_name_loss=True)
    assert "TAYITA" not in res["new_name"]
    assert "TAYITA" not in _name(path, pid)


def test_a_shrink_the_caller_causes_themselves_is_not_refused(
        editable_product, tmp_path):
    """Clearing `packaging_th` legitimately removes '(แผง)' from the name. The
    baseline is rebuilt BEFORE the update, so this is the caller's own edit and
    must go through — otherwise the guard blocks ordinary work."""
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"packaging_th": ""},
                          backup_dir=str(tmp_path / "b"))
    assert "(แผง)" not in res["new_name"], res["new_name"]


def test_an_underscore_only_difference_is_not_a_loss(editable_product, tmp_path):
    """`sub_category` legitimately stores 'แผ่นตัดเหล็กบาง_Super_Thin' while the
    stored name spells it with spaces — 137 active products differ ONLY that way
    and none of them loses information."""
    path, pid, _ = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET sub_category='กลอน_จัมโบ้', "
                 "product_name='กลอน จัมโบ้ Sendai #230-4in สีรมดำ (AC) (แผง)' WHERE id=?",
                 (pid,))
    conn.commit()
    conn.close()
    nc.save_product(path, pid, {"color_code": "CR"}, backup_dir=str(tmp_path / "b"))
    assert "จัมโบ้" in _name(path, pid)
