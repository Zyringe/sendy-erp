"""Card B (/mapping Smart Suggest) — family inheritance, brand short_code, and
the cost basis. Regression suite for the 2026-08-25 SONAX repair.

What went wrong on prod, and what each block here pins:

  * `create_structured_product` never wrote `family_id`, so every SKU born from
    a clone was family-less. -> test_clone_* below.
  * its inline brand INSERT copied `name` into `name_th` (picker rendered
    "SONAX / SONAX") and never set `short_code` -- which is a SEGMENT of
    sku_code, so the products lost their brand segment. -> test_brand_* below.
  * `bsn_suggest._latest_purchase` handed Card B `unit_price`: per BSN UNIT and
    BEFORE the discount. Prefilled into a product held in a finer unit it seeded
    `opening_cost` 20.8x too high, and `recalculate_product_wacc` keeps the last
    known WACC while stock is 0, so the correct purchase could never displace
    it. -> test_base_unit_cost_* below.

Every test asserts a COUNT or a CONTROL before the property under test, so an
empty/short-circuited setup shows up as a failure rather than a vacuous pass.
"""
import sqlite3

import pytest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _seed_category(db_path, code='chemical', name_th='สารเคมี', short_code='CHM'):
    c = _conn(db_path)
    c.execute("INSERT INTO categories (code, name_th, sort_order, short_code) "
              "VALUES (?,?,100,?)", (code, name_th, short_code))
    cid = c.execute("SELECT id FROM categories WHERE code=?", (code,)).fetchone()[0]
    c.commit(); c.close()
    return cid


def _seed_product(db_path, **cols):
    """Insert one product directly, forcing every column this test cares about.

    Deliberately NOT reusing create_structured_product: a fixture built by the
    code under test cannot show that code failing."""
    cols.setdefault('product_name', 'ต้นแบบ')
    cols.setdefault('unit_type', 'ตัว')
    keys = ', '.join(cols)
    marks = ', '.join('?' * len(cols))
    c = _conn(db_path)
    pid = c.execute(f"INSERT INTO products ({keys}) VALUES ({marks})",
                    list(cols.values())).lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?,0)", (pid,))
    c.commit(); c.close()
    return pid


# --------------------------------------------------------------------------
# family: a clone joins its template's family
# --------------------------------------------------------------------------
def test_clone_joins_the_template_family(empty_db):
    import models
    c = _conn(empty_db)
    fam = c.execute("INSERT INTO product_families (family_code, display_name) "
                    "VALUES ('SONAX-X','น้ำยา SONAX')").lastrowid
    c.commit(); c.close()
    src = _seed_product(empty_db, product_name='น้ำยา SONAX 200ml',
                        sku_code='CHM-200ml', family_id=fam, size='200ml')

    new_pid = models.create_structured_product(
        {'product_name': 'น้ำยา SONAX 300ml', 'size': '300ml',
         'clone_source_pid': src},
        'smart_mapping_clone_%d' % src)

    c = _conn(empty_db)
    # CONTROL first: the template must still be where we put it, otherwise the
    # assertion below could pass on a fixture that never had a family at all.
    assert c.execute("SELECT family_id FROM products WHERE id=?", (src,)).fetchone()[0] == fam
    assert c.execute("SELECT family_id FROM products WHERE id=?", (new_pid,)).fetchone()[0] == fam
    assert c.execute("SELECT COUNT(*) FROM product_families").fetchone()[0] == 1, \
        'joining an existing family must not mint another one'
    c.close()


def test_clone_from_familyless_template_mints_one_and_pulls_the_template_in(empty_db):
    import models
    src = _seed_product(empty_db, product_name='น้ำยา SONAX 300ml',
                        sku_code='CHM-300ml', size='300ml')
    c = _conn(empty_db)
    assert c.execute("SELECT COUNT(*) FROM product_families").fetchone()[0] == 0
    assert c.execute("SELECT family_id FROM products WHERE id=?", (src,)).fetchone()[0] is None
    c.close()

    new_pid = models.create_structured_product(
        {'product_name': 'น้ำยา SONAX 400ml', 'size': '400ml',
         'clone_source_pid': src}, 'smart_mapping_clone_%d' % src)

    c = _conn(empty_db)
    fams = c.execute("SELECT id, family_code, note FROM product_families").fetchall()
    assert len(fams) == 1, f'expected exactly one new family, got {len(fams)}'
    fam_id = fams[0]['id']
    assert fams[0]['family_code'] == 'CHM-300ml', fams[0]['family_code']
    assert fams[0]['note'] == models.SINGLETON_FAMILY_NOTE
    # the point of the feature: BOTH rows are siblings, not just the newcomer
    assert c.execute("SELECT family_id FROM products WHERE id=?", (src,)).fetchone()[0] == fam_id
    assert c.execute("SELECT family_id FROM products WHERE id=?", (new_pid,)).fetchone()[0] == fam_id
    c.close()


def test_explicit_family_id_wins_over_the_clone_source(empty_db):
    import models
    c = _conn(empty_db)
    fam_a = c.execute("INSERT INTO product_families (family_code, display_name) "
                      "VALUES ('A','A')").lastrowid
    fam_b = c.execute("INSERT INTO product_families (family_code, display_name) "
                      "VALUES ('B','B')").lastrowid
    c.commit(); c.close()
    src = _seed_product(empty_db, sku_code='SRC', family_id=fam_a)

    new_pid = models.create_structured_product(
        {'product_name': 'x', 'clone_source_pid': src, 'family_id': fam_b}, 'manual')

    c = _conn(empty_db)
    assert c.execute("SELECT family_id FROM products WHERE id=?", (src,)).fetchone()[0] == fam_a
    assert c.execute("SELECT family_id FROM products WHERE id=?", (new_pid,)).fetchone()[0] == fam_b
    c.close()


def test_no_clone_source_still_leaves_family_null(empty_db):
    """The historical behaviour has to survive: a hand-created product with no
    template gets no family. Without this, 'family is always set now' could be
    true for the wrong reason and nobody would notice."""
    import models
    pid = models.create_structured_product({'product_name': 'ของใหม่'}, 'manual')
    c = _conn(empty_db)
    assert c.execute("SELECT family_id FROM products WHERE id=?", (pid,)).fetchone()[0] is None
    assert c.execute("SELECT COUNT(*) FROM product_families").fetchone()[0] == 0
    c.close()


def test_ensure_product_family_never_moves_an_existing_family(empty_db):
    import models
    from database import get_connection
    c = _conn(empty_db)
    fam = c.execute("INSERT INTO product_families (family_code, display_name) "
                    "VALUES ('KEEP','keep')").lastrowid
    c.commit(); c.close()
    pid = _seed_product(empty_db, sku_code='P1', family_id=fam)

    conn = get_connection()
    got = models.ensure_product_family(conn, pid)
    conn.commit(); conn.close()

    assert got == fam
    c = _conn(empty_db)
    assert c.execute("SELECT COUNT(*) FROM product_families").fetchone()[0] == 1
    c.close()


# --------------------------------------------------------------------------
# brand: no name_th duplication, short_code reaches sku_code
# --------------------------------------------------------------------------
def test_new_brand_keeps_name_th_null_and_carries_short_code(empty_db):
    import models
    cat = _seed_category(empty_db)
    pid = models.create_structured_product({
        'product_name': 'น้ำยา SONAX 500ml',
        'category_id': cat,
        'sub_category_short_code': 'LIQ',
        'size': '500ml',
        'brand_other_name': 'SONAX',
        'brand_other_short_code': 'SONAX',
    }, 'smart_mapping')

    c = _conn(empty_db)
    brands = c.execute("SELECT id, name, name_th, short_code FROM brands").fetchall()
    assert len(brands) == 1, f'expected one brand, got {len(brands)}'
    assert brands[0]['name'] == 'SONAX'
    assert brands[0]['name_th'] is None, \
        'name_th must not be a copy of name — that is what rendered "SONAX / SONAX"'
    assert brands[0]['short_code'] == 'SONAX'
    # the payoff: sku_code regains its brand segment
    sku = c.execute("SELECT sku_code FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert sku == 'CHM-LIQ-SONAX-500ml', sku
    c.close()


def test_new_brand_thai_name_is_kept_when_supplied(empty_db):
    import models
    models.create_structured_product({
        'product_name': 'ค้อน',
        'brand_other_name': 'Tiger',
        'brand_other_name_th': 'ตราเสือ',
        'brand_other_short_code': 'TIGER',
    }, 'manual')
    c = _conn(empty_db)
    row = c.execute("SELECT name, name_th, short_code FROM brands").fetchone()
    assert (row['name'], row['name_th'], row['short_code']) == ('Tiger', 'ตราเสือ', 'TIGER')
    c.close()


def test_typing_an_existing_brand_name_reuses_it_instead_of_duplicating(empty_db):
    import models
    c = _conn(empty_db)
    c.execute("INSERT INTO brands (code, name, short_code, is_own_brand, sort_order) "
              "VALUES ('sonax','SONAX','SONAX',0,100)")
    c.commit(); c.close()

    models.create_structured_product({
        'product_name': 'x', 'brand_other_name': '  sonax  ',
    }, 'manual')

    c = _conn(empty_db)
    rows = c.execute("SELECT id, name, short_code FROM brands").fetchall()
    assert len(rows) == 1, f'a second brand row was created: {[dict(r) for r in rows]}'
    assert rows[0]['short_code'] == 'SONAX', 'reuse must not blank the existing short_code'
    c.close()


def test_derive_brand_short_code_shape():
    import models
    assert models.derive_brand_short_code('SONAX') == 'SONAX'
    assert models.derive_brand_short_code('Four Stars') == 'FOURST'   # capped at 6
    assert models.derive_brand_short_code('ตราช้าง') == ''            # no ASCII -> ask


# --------------------------------------------------------------------------
# cost: per BASE unit, from net, never the per-BSN-unit list price
# --------------------------------------------------------------------------
@pytest.mark.parametrize('net,qty,ratio,expected', [
    (1044.55, 1, 12, 87.04583333333333),   # SONAX 300ml, the real bill
    (1211.91, 1, 12, 100.9925),            # SONAX 500ml
    (240.0, 2, 1, 120.0),                  # same-unit line: divisor is just qty
])
def test_base_unit_cost_divides_net_by_qty_times_ratio(net, qty, ratio, expected):
    from bsn_suggest import base_unit_cost
    assert base_unit_cost(net, qty, ratio) == pytest.approx(expected)


@pytest.mark.parametrize('net,qty,ratio', [
    (100.0, 0, 12),      # no quantity
    (100.0, 1, 0),       # no ratio
    (100.0, 1, None),    # ratio not known yet
    (100.0, None, 12),
])
def test_base_unit_cost_returns_none_rather_than_a_wrong_number(net, qty, ratio):
    from bsn_suggest import base_unit_cost
    assert base_unit_cost(net, qty, ratio) is None


def test_latest_purchase_exposes_net_and_qty_not_just_list_price(empty_db):
    """The ×20.8 bug was invisible because only `unit_price` crossed this
    boundary. Assert the discount-aware figures are actually shipped."""
    from database import get_connection
    import bsn_suggest
    c = _conn(empty_db)
    c.execute("INSERT INTO purchase_transactions "
              "(bsn_code, product_name_raw, unit, qty, unit_price, net, date_iso, doc_no) "
              "VALUES ('999น0301','น้ำยา SONAX 300ml','โหล',1,1810.0,1044.55,'2026-08-15','RR1')")
    c.commit(); c.close()

    conn = get_connection()
    got = bsn_suggest._latest_purchase(conn, '999น0301')
    conn.close()

    assert got, 'fixture row was not found — the rest of this test would be vacuous'
    assert got['cost_price'] == 1810.0        # list price still available for display
    assert got['line_net'] == 1044.55
    assert got['last_qty'] == 1
    assert got['unit_type'] == 'โหล'
    # the number Card B must actually use
    assert bsn_suggest.base_unit_cost(
        got['line_net'], got['last_qty'], 12) == pytest.approx(87.0458333333)


# --------------------------------------------------------------------------
# Codex review 2026-08-25 — the same money defect at the OTHER door, plus the
# clone-source integrity rules
# --------------------------------------------------------------------------
def _seed_purchase(db_path, bsn_code, net, qty, unit='โหล', unit_price=None):
    c = _conn(db_path)
    c.execute(
        "INSERT INTO purchase_transactions "
        "(bsn_code, product_name_raw, unit, qty, unit_price, net, date_iso, doc_no) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (bsn_code, 'ราคาทดสอบ', unit, qty, unit_price if unit_price is not None else net,
         net, '2026-08-15', 'RR-TEST'))
    c.commit(); c.close()


def _stage(db_path, **over):
    """Stage one suggestion straight into the table, forcing every column the
    cost logic reads. Not via save_pending_suggestion: a fixture built by code
    adjacent to the code under test can mask a change in it."""
    import models
    payload = {
        'bsn_code': 'TSTCOST1', 'bsn_name': 'ของทดสอบ',
        'suggested_name': 'ของทดสอบ', 'suggested_cost': 87.0458,
        'suggested_unit_type': 'ตัว', 'bsn_unit': 'โหล',
        'unit_conversion_ratio': 12,
        # every remaining NOT-defaulted binding, spelled out rather than
        # inherited: save_pending_suggestion only setdefaults its "extras"
        'category': None, 'series': None, 'brand_id': None, 'model': None,
        'size': None, 'color_th': None, 'color_code': None, 'packaging': None,
        'condition': None, 'pack_variant': None,
        'units_per_carton': None, 'units_per_box': None,
    }
    payload.update(over)
    return models.save_pending_suggestion(payload, user_id=None)


def test_approve_reprices_when_the_manager_corrects_the_ratio(empty_db):
    """The staged cost was net/(qty x 12). Approving with ratio 24 must halve
    it — otherwise the wrong number is written to cost_price AND opening_cost,
    and the WACC walk keeps it (zero-stock branch) past the first real
    purchase. Found by Codex review; the Card-B fix alone did not cover it."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db)

    pid = models.approve_pending_suggestion(
        sid, {'unit_conversion_ratio': 24}, reviewer_id=None)

    c = _conn(empty_db)
    row = c.execute("SELECT cost_price, opening_cost FROM products WHERE id=?",
                    (pid,)).fetchone()
    c.close()
    expected = 1044.55 / (1 * 24)
    assert row['cost_price'] == pytest.approx(expected), (
        f"got {row['cost_price']} — a ratio corrected at approve time must "
        f"move the cost with it")
    assert row['opening_cost'] == pytest.approx(expected), (
        'opening_cost seeds the WACC walk — it must carry the same basis')


def test_approve_keeps_an_unchanged_ratio_alone(empty_db):
    """CONTROL for the test above: without this, 'the cost changed' could not
    be told apart from 'the cost is always recomputed'."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db)
    pid = models.approve_pending_suggestion(sid, {}, reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(87.0458)


def test_approve_never_overrides_a_retyped_cost(empty_db):
    """A cost the manager typed is their call, even when the ratio also moved."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db)
    pid = models.approve_pending_suggestion(
        sid, {'unit_conversion_ratio': 24, 'suggested_cost': 55.0}, reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(55.0)


def test_approve_with_no_purchase_row_keeps_the_staged_cost(empty_db):
    """No basis to derive from — the staged number must survive rather than
    being zeroed."""
    import models
    sid = _stage(empty_db)          # deliberately no purchase row seeded
    pid = models.approve_pending_suggestion(
        sid, {'unit_conversion_ratio': 24}, reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(87.0458)


def test_clone_from_an_inactive_template_does_not_mint_a_family_from_it(empty_db):
    """A retired SKU still LENDS a family it belongs to, but one is never
    minted from it — that would name the family after a dead code and write
    family_id onto a row nobody is looking at."""
    import models
    src = _seed_product(empty_db, product_name='ของเก่า', sku_code='OLD-1', is_active=0)
    new_pid = models.create_structured_product(
        {'product_name': 'ของใหม่', 'clone_source_pid': src}, 'manual_clone_%d' % src)
    c = _conn(empty_db)
    assert c.execute("SELECT COUNT(*) FROM product_families").fetchone()[0] == 0, \
        'no family may be minted from an inactive template'
    assert c.execute("SELECT family_id FROM products WHERE id=?", (src,)).fetchone()[0] is None, \
        'the inactive template must not be mutated'
    assert c.execute("SELECT family_id FROM products WHERE id=?", (new_pid,)).fetchone()[0] is None
    c.close()


def test_clone_from_an_inactive_template_that_HAS_a_family_still_joins_it(empty_db):
    """The other half — deactivating a product does not dissolve its grouping,
    so joining it is correct. Without this the rule above would read as
    'inactive templates are ignored', which is not what was decided."""
    import models
    c = _conn(empty_db)
    fam = c.execute("INSERT INTO product_families (family_code, display_name) "
                    "VALUES ('OLDFAM','เก่า')").lastrowid
    c.commit(); c.close()
    src = _seed_product(empty_db, sku_code='OLD-2', is_active=0, family_id=fam)
    new_pid = models.create_structured_product(
        {'product_name': 'ของใหม่', 'clone_source_pid': src}, 'manual_clone_%d' % src)
    c = _conn(empty_db)
    assert c.execute("SELECT family_id FROM products WHERE id=?", (new_pid,)).fetchone()[0] == fam
    c.close()


def test_a_clone_source_that_does_not_exist_is_refused(empty_db):
    """Silently producing a family-less 'clone' whose created_via still names a
    template is a lie in the provenance column."""
    import models
    with pytest.raises(ValueError, match='clone_source_pid'):
        models.create_structured_product(
            {'product_name': 'x', 'clone_source_pid': 999999}, 'manual_clone_999999')
    c = _conn(empty_db)
    assert c.execute("SELECT COUNT(*) FROM products WHERE product_name='x'").fetchone()[0] == 0, \
        'the refusal must leave no orphan product behind'
    c.close()


def test_brand_reuse_is_deterministic_and_the_product_gets_that_brand(empty_db):
    """Closes the Codex note that the reuse test never checked which brand the
    PRODUCT ended up on: returning None while leaving the table untouched would
    have passed the old assertion."""
    import models
    c = _conn(empty_db)
    first = c.execute("INSERT INTO brands (code, name, short_code, is_own_brand, sort_order) "
                      "VALUES ('sonax','SONAX','SONAX',0,100)").lastrowid
    # a second row with the same display name — only possible for legacy data,
    # which is exactly when determinism matters
    c.execute("INSERT INTO brands (code, name, short_code, is_own_brand, sort_order) "
              "VALUES ('sonax_2','SONAX','SNX2',0,100)")
    c.commit(); c.close()

    pid = models.create_structured_product(
        {'product_name': 'x', 'brand_other_name': 'SONAX'}, 'manual')

    c = _conn(empty_db)
    assert c.execute("SELECT COUNT(*) FROM brands").fetchone()[0] == 2, \
        'reuse must not add a third row'
    assert c.execute("SELECT brand_id FROM products WHERE id=?", (pid,)).fetchone()[0] == first, \
        'oldest row wins, every time'
    c.close()


# --------------------------------------------------------------------------
# Review round 2 — the cost BASIS is (ratio AND base unit), and "did a human
# type this?" is a fact the client reports, never one the server infers
# --------------------------------------------------------------------------
def test_changing_only_the_base_unit_moves_the_cost(empty_db):
    """The ratio field never moves, but switching the product from ตัว to โหล
    changes the correct cost by 12x: at โหล the BSN unit IS the base unit, so
    the divisor is 1. Comparing ratios alone missed this entirely."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1, unit='โหล')
    sid = _stage(empty_db)          # staged as ตัว, ratio 12 -> 87.0458

    pid = models.approve_pending_suggestion(
        sid, {'suggested_unit_type': 'โหล'}, reviewer_id=None)

    c = _conn(empty_db)
    row = c.execute("SELECT unit_type, cost_price FROM products WHERE id=?",
                    (pid,)).fetchone()
    c.close()
    assert row['unit_type'] == 'โหล'
    assert row['cost_price'] == pytest.approx(1044.55), (
        f"got {row['cost_price']} — one โหล costs the whole line, not 1/12 of it"
    )


def test_changing_unit_and_ratio_together_uses_the_effective_divisor(empty_db):
    """Base unit == BSN unit wins over whatever the ratio box says: no
    conversion applies, so a stale ratio must not divide the cost again."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1, unit='โหล')
    sid = _stage(empty_db)
    pid = models.approve_pending_suggestion(
        sid, {'suggested_unit_type': 'โหล', 'unit_conversion_ratio': 24},
        reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(1044.55)


def test_an_explicit_dirty_flag_protects_a_retyped_identical_cost(empty_db):
    """The case numeric comparison cannot see: the operator deliberately types
    the same number the derivation produced, then corrects the ratio. Without
    the flag the server reads 'unchanged' and overwrites a deliberate value."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db)
    pid = models.approve_pending_suggestion(
        sid,
        {'unit_conversion_ratio': 24,
         'suggested_cost': 87.0458,          # byte-identical to the staged value
         'suggested_cost_dirty': True},
        reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(87.0458), (
        f'got {got} — an explicitly-flagged hand-typed cost must survive'
    )


def test_without_the_flag_the_same_edit_is_still_recomputed(empty_db):
    """CONTROL for the test above. If this passed too, the flag would be
    decoration and the test above would prove nothing."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db)
    pid = models.approve_pending_suggestion(
        sid, {'unit_conversion_ratio': 24, 'suggested_cost': 87.0458},
        reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(1044.55 / 24)


def test_effective_ratio_folds_the_base_unit_in():
    from models.suggestions import _effective_ratio
    assert _effective_ratio('ตัว', 'โหล', 12) == 12        # conversion applies
    assert _effective_ratio('โหล', 'โหล', 12) == 1.0       # same unit -> no divide
    assert _effective_ratio('ตัว', None, 12) == 1.0        # no BSN unit known
    assert _effective_ratio('ตัว', 'โหล', None) is None    # ratio not known yet


# --------------------------------------------------------------------------
# Review round 3 — defects introduced BY the round-2 fixes
# --------------------------------------------------------------------------
def test_a_cleared_unit_box_does_not_divide_the_cost(empty_db):
    """`_effective_ratio` must normalise the unit the same way the INSERT does
    (`or 'ตัว'`). A cleared unit box compared '' against bsn_unit 'ตัว',
    concluded a conversion applied, and divided by the ratio for a product
    saved AS 'ตัว' — 12x too low."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=240.0, qty=2, unit='ตัว')
    sid = _stage(empty_db, bsn_unit='ตัว', unit_conversion_ratio=1,
                 suggested_cost=120.0)
    pid = models.approve_pending_suggestion(
        sid, {'suggested_unit_type': '', 'unit_conversion_ratio': 12},
        reviewer_id=None)
    c = _conn(empty_db)
    row = c.execute("SELECT unit_type, cost_price FROM products WHERE id=?",
                    (pid,)).fetchone()
    c.close()
    assert row['unit_type'] == 'ตัว', 'the INSERT defaults a blank unit to ตัว'
    assert row['cost_price'] == pytest.approx(120.0), (
        f"got {row['cost_price']} — the product is held in the unit the BSN "
        f"bills in, so no conversion applies and the ratio box is irrelevant")


def test_effective_ratio_normalises_a_blank_unit():
    from models.suggestions import _effective_ratio
    assert _effective_ratio('', 'ตัว', 12) == 1.0
    assert _effective_ratio(None, 'ตัว', 12) == 1.0
    assert _effective_ratio('  ', 'ตัว', 12) == 1.0
    assert _effective_ratio('ตัว', '', 12) == 1.0      # no BSN unit known
    assert _effective_ratio(' โหล ', 'โหล', 12) == 1.0  # padded == same unit


@pytest.mark.parametrize('given,stored', [
    ('  ', 'ตัว'), ('', 'ตัว'), (None, 'ตัว'), (' โหล ', 'โหล'), ('ตัว', 'ตัว'),
])
def test_the_unit_that_is_COSTED_is_the_unit_that_is_STORED(empty_db, given, stored):
    """The costing helper and the INSERT must agree by construction. They used
    not to: `d.get('unit_type') or 'ตัว'` persisted '  ' verbatim (two spaces
    are truthy) while `_effective_ratio` read it as 'ตัว' and dropped the
    conversion divisor. Asserting the helper alone could never see that —
    this asserts the PERSISTED value (round 4)."""
    import models
    from models.suggestions import _effective_ratio
    pid = models.create_structured_product(
        {'product_name': 'ของทดสอบหน่วย', 'unit_type': given}, 'manual')
    c = _conn(empty_db)
    got = c.execute("SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == stored, f'stored {got!r}, expected {stored!r}'
    # and the costing helper reaches the SAME verdict for that stored unit
    assert _effective_ratio(given, stored, 12) == _effective_ratio(stored, stored, 12) == 1.0


def test_an_unknown_staged_ratio_never_justifies_a_rewrite(empty_db):
    """Staging with no ratio and supplying one at approval does not prove the
    staged cost was derived from the old basis — it may have been typed by
    hand before any dirty flag existed. Preserve it."""
    import models
    _seed_purchase(empty_db, 'TSTCOST1', net=1044.55, qty=1)
    sid = _stage(empty_db, unit_conversion_ratio=None, suggested_cost=100.0)
    pid = models.approve_pending_suggestion(
        sid, {'unit_conversion_ratio': 12}, reviewer_id=None)
    c = _conn(empty_db)
    got = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    c.close()
    assert got == pytest.approx(100.0), (
        f'got {got} — a staged cost with no known basis must survive')


class _RacingConn:
    """Wraps a connection and inserts a COMPETING brand row the first time an
    INSERT INTO brands is attempted — i.e. exactly between upsert_brand's
    code-uniqueness loop and its own INSERT.

    Needed because the ON CONFLICT DO NOTHING branch is unreachable
    sequentially: called one after another, the uniqueness loop already sees
    the taken code and picks `acme_2`. Only a concurrent writer (real on prod:
    `gunicorn -w 2`) can make the INSERT lose. Patching the seam is the
    documented alternative to racing threads.
    """

    def __init__(self, conn, competitor_sql):
        self._conn = conn
        self._competitor = competitor_sql
        self.fired = False

    def execute(self, sql, *a, **kw):
        if not self.fired and 'INSERT INTO brands' in sql:
            self.fired = True
            self._conn.execute(self._competitor)
        return self._conn.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_losing_the_code_race_to_a_DIFFERENT_name_does_not_reuse_it(empty_db):
    """'ACME!' and 'ACME?' both slugify to 'acme'. Losing the race must NOT
    attach this product to the winner — that is a different brand, with a
    different short_code and own-brand flag."""
    import models
    from database import get_connection
    conn = get_connection()
    racer = _RacingConn(
        conn,
        "INSERT INTO brands (code, name, short_code, is_own_brand, sort_order) "
        "VALUES ('acme', 'ACME?', 'ACME2', 0, 100)",
    )
    new_id = models.upsert_brand(racer, 'ACME!', short_code='ACME1')
    conn.commit()

    assert racer.fired, 'the seam never fired — this test proved nothing'
    rows = {r['id']: dict(r) for r in
            conn.execute("SELECT id, code, name, short_code FROM brands").fetchall()}
    conn.close()
    assert len(rows) == 2, rows
    assert rows[new_id]['name'] == 'ACME!', (
        f'got {rows[new_id]} — losing the code race must not hand back the '
        f'winner, whose name is a different brand entirely')
    assert rows[new_id]['short_code'] == 'ACME1'
    assert rows[new_id]['code'] != 'acme', 'the loser needs its own code'


def test_the_same_name_losing_the_race_DOES_reuse_the_winner(empty_db):
    """The other half: an identical name is the same brand, so reusing the
    winner is correct — that is the whole point of tolerating the race."""
    import models
    from database import get_connection
    conn = get_connection()
    racer = _RacingConn(
        conn,
        "INSERT INTO brands (code, name, short_code, is_own_brand, sort_order) "
        "VALUES ('acme', 'ACME', 'ACME', 0, 100)",
    )
    got = models.upsert_brand(racer, 'ACME', short_code='ACME')
    conn.commit()
    assert racer.fired
    rows = conn.execute("SELECT id, name FROM brands").fetchall()
    conn.close()
    assert len(rows) == 1, [dict(r) for r in rows]
    assert got == rows[0]['id']
