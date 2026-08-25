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


def test_save_never_touches_sku_code(editable_product, tmp_path):
    """The core of #383. `SEED-1` is nothing like what the generator would
    produce for this product (`SD-#230-4in-CR-PN`), so this fixture has exactly
    the drift that made real saves dangerous."""
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, sku_code, color_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()

    # CONTROL FIRST: the save must actually have done its job, or "sku_code did
    # not change" would pass on a no-op that never reached the code under test.
    # CONTROL moved to the COLUMN (2026-08-25): the name is the source of truth now and
    # a naming save no longer rewrites it, so "the name changed" can no longer prove the
    # save reached the code under test.
    assert row["color_code"] == "CR"
    assert res["new_name"] == row["product_name"], "a naming save must not move the name"

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
    assert row["packaging_short"] == "UN"                      # ตัว → UN derived — THE SUBJECT
    # (the old `product_name.endswith("(ตัว)")` assertion went with the rebuild)
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
    row = conn.execute("SELECT color_code, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    assert row["color_code"] == "CR"                            # control: the save landed
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
    row = conn.execute("SELECT size, sku_code FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    # CONTROL: the edit really landed, so the sku assertion is not on a no-op. Asserts
    # the COLUMN — a naming save no longer rewrites the name (2026-08-25).
    assert row["size"] == "6in", row["size"]
    assert row["sku_code"] == "ZZZ-383-ROUTE", "the route must not move the join key"
    assert body["sku_code"] == "ZZZ-383-ROUTE"


# ── the name is the SOURCE OF TRUTH (2026-08-25) ─────────────────────────────
# save_product used to recompose product_name from the columns on every save and UPDATE
# it unconditionally. Measured on the 2026-08-24 prod snapshot, 342 of 1,994 active
# products carry text no column holds, so that silently destroyed it — and the audit log
# shows all 2,594 recorded name changes were DIRECT writes with no naming-column edit,
# i.e. the naming standard has always been enforced by scripts, never by this rebuild.
# Put confirmed the single-product workbench is "แทบไม่ได้ใช้เลย".


def _name(path, pid):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row[0]


def test_editing_a_column_does_NOT_touch_the_name(editable_product, tmp_path):
    """The whole point. Changing colour used to rewrite the name from the columns."""
    path, pid, _ = editable_product
    before = _name(path, pid)
    res = nc.save_product(path, pid, {"color_code": "CR"}, backup_dir=str(tmp_path / "b"))

    assert res["new_name"] == before
    assert _name(path, pid) == before
    # CONTROL — the column really did change, so this is not "the save did nothing".
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT color_code FROM products WHERE id=?", (pid,)).fetchone()[0] == "CR"
    conn.close()


def test_a_hand_tuned_name_survives_a_column_edit(editable_product, tmp_path):
    """The prod shape: 'TAYITA' lives in no column, so a rebuild would drop it."""
    path, pid, _ = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET product_name=? WHERE id=?",
                 ("กลอน TAYITA Sendai #230-4in สีรมดำ (AC) (แผง)", pid))
    conn.commit()
    conn.close()

    nc.save_product(path, pid, {"color_code": "CR"}, backup_dir=str(tmp_path / "b"))
    assert "TAYITA" in _name(path, pid)


def test_an_explicit_name_is_what_gets_stored(editable_product, tmp_path):
    """How the operator (or the workbench's adopt-suggestion button) changes a name."""
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"product_name": "  ชื่อใหม่ที่พิมพ์เอง  "},
                          backup_dir=str(tmp_path / "b"))
    assert res["new_name"] == "ชื่อใหม่ที่พิมพ์เอง"          # trimmed
    assert _name(path, pid) == "ชื่อใหม่ที่พิมพ์เอง"


def test_a_blank_explicit_name_is_ignored_not_stored(editable_product, tmp_path):
    """An empty box must never blank a product's name."""
    path, pid, _ = editable_product
    before = _name(path, pid)
    nc.save_product(path, pid, {"product_name": "   "}, backup_dir=str(tmp_path / "b"))
    assert _name(path, pid) == before


def test_rebuild_name_opt_in_still_recomposes(editable_product, tmp_path):
    """scripts/hammer_bundle_datafix.py W1 depends on this and asserts the exact result;
    removing the default rebuild must not remove the capability."""
    path, pid, _ = editable_product
    res = nc.save_product(path, pid, {"color_code": "CR"},
                          backup_dir=str(tmp_path / "b"), rebuild_name=True)
    assert "สีโครเมียม" in res["new_name"], res["new_name"]
    assert _name(path, pid) == res["new_name"]


def test_the_ROUTE_stores_the_typed_name_and_leaves_it_alone_otherwise(admin_client, tmp_db):
    """Route level: a column-only save must not move the name, and a typed name must land."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM products WHERE sku_code='ZZZ-TRUTH-ROUTE'")
    bid = conn.execute("SELECT id FROM brands WHERE short_code='SD'").fetchone()[0]
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size,"
        "                     packaging_th, packaging_short, sku_code, is_active)"
        " VALUES ('กลอน TAYITA Sendai #902-4in (แผง)', ?, 'กลอน', '#902', '4in',"
        "         'แผง', 'PN', 'ZZZ-TRUTH-ROUTE', 1)", (bid,)).lastrowid
    conn.commit()
    conn.close()

    r = admin_client.post(f'/naming/product/{pid}/save', json={"size": "6in"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert "TAYITA" in _name(tmp_db, pid)

    r2 = admin_client.post(f'/naming/product/{pid}/save',
                           json={"product_name": "กลอน TAYITA Sendai #902-6in (แผง)"})
    assert r2.status_code == 200
    assert _name(tmp_db, pid) == "กลอน TAYITA Sendai #902-6in (แผง)"


def test_the_preview_route_reports_what_adopting_the_suggestion_would_drop(
        admin_client, tmp_db):
    """The workbench shows this beside the button, so replacing a hand-tuned name is a
    visible choice instead of something a save does behind the operator's back."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM products WHERE sku_code='ZZZ-PREVIEW-LOSS'")
    bid = conn.execute("SELECT id FROM brands WHERE short_code='SD'").fetchone()[0]
    pid = conn.execute(
        "INSERT INTO products(product_name, brand_id, sub_category, model, size,"
        "                     packaging_th, packaging_short, sku_code, is_active)"
        " VALUES ('กลอน TAYITA Sendai #903-4in (แผง)', ?, 'กลอน', '#903', '4in',"
        "         'แผง', 'PN', 'ZZZ-PREVIEW-LOSS', 1)", (bid,)).lastrowid
    conn.commit()
    conn.close()

    r = admin_client.post('/naming/product/preview-name', json={
        "pid": pid, "sub_category": "กลอน", "brand_id": bid, "model": "#903",
        "size": "4in", "packaging_th": "แผง"})
    body = r.get_json()
    assert body["ok"] is True
    assert body["loss"] == ["TAYITA"], body
    assert "TAYITA" in body["stored_name"]
    assert "TAYITA" not in body["name"]


def test_the_generated_name_never_gains_a_literal_underscore(editable_product, tmp_path):
    """196 active `series` values hold a literal underscore; the composer spliced it into
    the name raw, so adopting a suggestion would put 'DEAD_LOCK' in a customer-facing
    name. ZERO stored names contain an underscore, so normalising is provably safe."""
    path, pid, _ = editable_product
    conn = sqlite3.connect(path)
    conn.execute("UPDATE products SET series='DEAD_LOCK' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    res = nc.save_product(path, pid, {}, backup_dir=str(tmp_path / "b"), rebuild_name=True)
    assert "_" not in res["new_name"], res["new_name"]
    assert "DEAD LOCK" in res["new_name"], res["new_name"]


@pytest.mark.parametrize("stored,generated,expected", [
    (None, "กลอน Sendai", []),                       # nothing stored -> nothing to lose
    ("", "กลอน Sendai", []),
    ("กลอน Sendai", None, ["กลอน Sendai"]),          # rebuild returned nothing at all
    ("กลอน Sendai", "", ["กลอน Sendai"]),            # prod pid 1994 (OTH-FILER): every
                                                     # naming column NULL -> empty name
    ("ABC", "XYZ", ["ABC"]),                         # wholly replaced
    ("กลอน", "กลอน Sendai #230 (แผง)", []),           # candidate LONGER: a gain, not a loss
    ("###", "#", []),                                # punctuation-only delta
    # Thai สระ/วรรณยุกต์ are combining marks (category Mn), NOT punctuation. Dropping
    # one changes the word, and an L/N-only content test reported "nothing lost".
    # The fragment is the MARK alone (only it was deleted); the refusal also carries
    # old_name/new_name, which is what makes a bare diacritic readable to an operator.
    ("กิ", "ก", ["ิ"]),                               # สระอิ destroyed
    ("ก่", "ก", ["่"]),                               # ไม้เอก destroyed
    ("สีรมดำ", "สีรมดา", ["ำ"]),                       # sanity: an Lo change was caught
    # ZWJ/ZWNJ are Cf, not L/M/N. Dropping one changes shaping, so they are content —
    # explicitly, not the whole Cf category (RLM/LRM and SOFT HYPHEN really are format).
    ("ก\u200dข", "กข", ["\u200d"]),                   # ZERO WIDTH JOINER destroyed
    ("ก\u200cข", "กข", ["\u200c"]),                   # ZERO WIDTH NON-JOINER destroyed
    # Canonically EQUIVALENT, marks in a different order. Without NFC this reported a
    # deleted mark and refused a save that could never have lost anything.
    ("x\u0315\u0300", "x\u0300\u0315", []),
    ("  กลอน   Sendai  ", "กลอน Sendai", []),         # whitespace-only delta
    ("a_b", "a b", []),                              # underscore-only delta
])
def test_name_loss_edge_cases(stored, generated, expected):
    """`rebuild_product_name` returns None for a missing row and "" when every naming
    column is NULL, and a candidate can be longer than the stored name. None of these
    may raise, and none may be silently treated as "no loss" when text really goes."""
    assert nc._name_loss(stored, generated) == expected
