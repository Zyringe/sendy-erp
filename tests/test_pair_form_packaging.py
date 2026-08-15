"""/conversions/pair — teach the pack↔loose pair form (and the
cross_unit_hazard guard) about an optional packaging component, per
docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5 Phase 2. Built on mig
158's conversion_formula_inputs.role + models/conversion_roles.py (already
merged — see tests/test_conversion_roles.py for the validator's own unit
tests; this file exercises the CALLERS: upsert_pack_unpack_pair,
derive_pair_from_formula, cross_unit_hazard, and the /conversions/pair route).
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models
from models.conversion_roles import ROLE_COMPONENT, ROLE_PACKAGING, ConversionRoleError


# ── shared helpers (mirrors tests/test_conversion_pair.py's style) ─────────

def _seed_product(conn, pid, name, unit="ตัว"):
    conn.execute("INSERT INTO products (id, product_name, unit_type) VALUES (?, ?, ?)", (pid, name, unit))


def _active_formulas(conn):
    return conn.execute(
        "SELECT id, name, output_product_id, output_qty FROM conversion_formulas WHERE is_active=1"
    ).fetchall()


def _inputs(conn, fid):
    """product_id -> (quantity, role), keyed (never positional — role exists
    precisely because row order carries no meaning on this table)."""
    return {r["product_id"]: (r["quantity"], r["role"]) for r in conn.execute(
        "SELECT product_id, quantity, role FROM conversion_formula_inputs WHERE formula_id=?", (fid,))}


PACK, LOOSE, CARD = 101, 102, 103


def _seed_bundle_products(conn):
    _seed_product(conn, PACK, "hammer pack", "แผง")
    _seed_product(conn, LOOSE, "hammer loose", "อัน")
    _seed_product(conn, CARD, "blister card", "แผง")
    conn.commit()


# ── upsert_pack_unpack_pair(packaging_id=...) ───────────────────────────────

def test_bundle_creates_two_role_inputs_no_unpack_half(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    res = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c, packaging_id=CARD)
    c.commit()
    assert res["created"] == 1                              # only the pack half — no [แกะ]
    fs = _active_formulas(c)
    assert len(fs) == 1
    f = fs[0]
    assert f["output_product_id"] == PACK and f["output_qty"] == 1
    assert f["name"].startswith('[แพ็ค]') and 'blister card' in f["name"]
    assert _inputs(c, f["id"]) == {LOOSE: (1, ROLE_COMPONENT), CARD: (1, ROLE_PACKAGING)}


def test_plain_pair_unchanged_role_null_both_halves(empty_db_conn):
    """No packaging_id -> byte-identical to today: single input, role NULL,
    both halves created."""
    c = empty_db_conn
    _seed_product(c, PACK, "pack", "แผง")
    _seed_product(c, LOOSE, "loose", "ตัว")
    c.commit()
    res = models.upsert_pack_unpack_pair(PACK, LOOSE, 2, direction='both', conn=c)
    c.commit()
    assert res["created"] == 2
    fs = {f["output_product_id"]: f["id"] for f in _active_formulas(c)}
    assert set(fs) == {PACK, LOOSE}
    assert _inputs(c, fs[PACK]) == {LOOSE: (2, None)}
    assert _inputs(c, fs[LOOSE]) == {PACK: (1, None)}


def test_bundle_direction_forced_pack_even_if_both_requested(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    res = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c, packaging_id=CARD)
    c.commit()
    assert res["created"] == 1
    assert len(_active_formulas(c)) == 1                     # no [แกะ] regardless of direction='both'


def test_bundle_round_trip_save_derive_save_unchanged(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    res1 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c, packaging_id=CARD)
    c.commit()
    assert len(res1['formula_ids']) == 1
    fid = res1['formula_ids'][0]

    derived = models.derive_pair_from_formula(fid, conn=c)
    assert derived is not None
    assert derived['pack_id'] == PACK
    assert derived['loose_id'] == LOOSE
    assert derived['ratio'] == 1
    assert derived['direction'] == 'pack'
    assert derived['packaging_id'] == CARD
    assert derived['packaging_name'] == 'blister card'

    res2 = models.upsert_pack_unpack_pair(
        derived['pack_id'], derived['loose_id'], derived['ratio'], direction='both',
        conn=c, packaging_id=derived['packaging_id'])
    c.commit()
    assert res2['created'] == 0 and res2['updated'] == 1
    assert res2['formula_ids'] == [fid]                      # same formula, updated in place
    assert len(_active_formulas(c)) == 1
    assert _inputs(c, fid) == {LOOSE: (1, ROLE_COMPONENT), CARD: (1, ROLE_PACKAGING)}


def test_bundle_input_rows_reverse_order_still_derives_same_component(empty_db_conn):
    """The whole reason role exists: row order must never carry meaning.
    Build the formula by hand with the PACKAGING row inserted first, and
    prove derive_pair_from_formula still resolves loose_id to the component
    row rather than the row that happened to be inserted (or come back) first."""
    c = empty_db_conn
    _seed_bundle_products(c)
    fid = c.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        ("[แพ็ค] hammer pack ⟵ 1 อัน + blister card", PACK, 1)).lastrowid
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
              (fid, CARD, 1, ROLE_PACKAGING))                 # packaging inserted FIRST
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
              (fid, LOOSE, 1, ROLE_COMPONENT))
    c.commit()

    derived = models.derive_pair_from_formula(fid, conn=c)
    assert derived is not None
    assert derived['loose_id'] == LOOSE                       # not CARD (the first-inserted row)
    assert derived['packaging_id'] == CARD


def test_switching_plain_pair_to_bundle_deactivates_reciprocal_unpack(empty_db_conn):
    """Codex review finding 1 (blocker on PR #388): a blister card is
    destroyed on opening, so a bundle has no real [แกะ] to recover through —
    converting a plain pair into a bundle must not leave the OLD [แกะ] active
    and runnable. Deactivate (never delete — the audit history survives),
    in the SAME transaction as the [แพ็ค] update."""
    c = empty_db_conn
    _seed_bundle_products(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c)
    c.commit()
    pack_fid = next(f['id'] for f in _active_formulas(c) if f['output_product_id'] == PACK)
    unpack_before = [f['id'] for f in _active_formulas(c) if f['output_product_id'] == LOOSE]
    assert len(unpack_before) == 1

    res2 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c, packaging_id=CARD)
    c.commit()
    assert res2['created'] == 0 and res2['updated'] == 1
    assert res2['deactivated'] == 1                            # reported so the flash can say what happened
    assert res2['deactivated_ids'] == unpack_before

    pack_rows = [f for f in _active_formulas(c) if f['output_product_id'] == PACK]
    assert len(pack_rows) == 1                                # never two active [แพ็ค] for one output
    assert pack_rows[0]['id'] == pack_fid                     # same formula, updated in place
    assert _inputs(c, pack_fid) == {LOOSE: (1, ROLE_COMPONENT), CARD: (1, ROLE_PACKAGING)}

    # the pre-existing [แกะ] half must no longer be active/runnable...
    unpack_after_active = [f['id'] for f in _active_formulas(c) if f['output_product_id'] == LOOSE]
    assert unpack_after_active == []
    # ...but it must still EXIST (deactivated, not deleted) for audit history.
    row = c.execute("SELECT is_active FROM conversion_formulas WHERE id=?",
                    (unpack_before[0],)).fetchone()
    assert row is not None and row['is_active'] == 0


def test_new_bundle_with_no_prior_pack_leaves_deactivated_count_zero(empty_db_conn):
    """Control: a bundle created where no [แพ็ค] existed before (a fresh
    INSERT, not an UPDATE) has nothing to deactivate."""
    c = empty_db_conn
    _seed_bundle_products(c)
    res = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='both', conn=c, packaging_id=CARD)
    c.commit()
    assert res['created'] == 1
    assert res.get('deactivated', 0) == 0
    assert res.get('deactivated_ids', []) == []


def test_switching_plain_pair_to_bundle_does_not_violate_unique_index(empty_db_conn):
    """Break-it-once target: dedup-by-output-alone for the [แพ็ค] spec is what
    makes this safe. Reverting to the old dedup key (output, frozenset(inputs))
    would miss the existing plain-pair formula (different input set) and try
    to INSERT a second active [แพ็ค] for PACK, raising sqlite3.IntegrityError
    against mig 158's ux_conv_active_pack_per_output."""
    c = empty_db_conn
    _seed_bundle_products(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c)
    c.commit()
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)  # must not raise
    c.commit()
    fs = [f for f in _active_formulas(c) if f['output_product_id'] == PACK]
    assert len(fs) == 1


# ── [แพ็ค] dedup must never silently re-point to a different component ─────
# Codex review finding 2 (blocker on PR #388): dedup now matches on (output,
# name LIKE '[แพ็ค]%') alone, so saving with a DIFFERENT loose product would
# silently rewire an existing stock recipe onto a different SKU. Allowed
# in-place updates are only: same component with a changed packaging/ratio/
# note, plain<->bundle of the SAME component. A different component must
# refuse — the operator has to delete/deactivate the existing formula first.

OTHER_LOOSE, OTHER_CARD = 104, 105


def _seed_second_loose_and_card(conn):
    _seed_product(conn, OTHER_LOOSE, "other loose", "อัน")
    _seed_product(conn, OTHER_CARD, "other card", "แผง")
    conn.commit()


def test_dedup_refuses_repoint_plain_pair_to_different_loose(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    _seed_second_loose_and_card(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c)
    c.commit()
    pack_fid = next(f['id'] for f in _active_formulas(c) if f['output_product_id'] == PACK)

    with pytest.raises(ConversionRoleError):
        models.upsert_pack_unpack_pair(PACK, OTHER_LOOSE, 1, direction='pack', conn=c)
    c.rollback()

    # nothing rewired — the existing formula still points at the ORIGINAL loose
    assert _inputs(c, pack_fid) == {LOOSE: (1, None)}


def test_dedup_refuses_repoint_bundle_to_different_loose(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    _seed_second_loose_and_card(c)
    res = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    pack_fid = res['formula_ids'][0]

    with pytest.raises(ConversionRoleError):
        models.upsert_pack_unpack_pair(PACK, OTHER_LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.rollback()

    assert _inputs(c, pack_fid) == {LOOSE: (1, ROLE_COMPONENT), CARD: (1, ROLE_PACKAGING)}


def test_dedup_allows_changing_packaging_same_component(empty_db_conn):
    """The one legitimate in-place edit this guard must NOT block: same
    component (LOOSE), only the packaging material changes."""
    c = empty_db_conn
    _seed_bundle_products(c)
    _seed_second_loose_and_card(c)
    res1 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    fid = res1['formula_ids'][0]

    res2 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=OTHER_CARD)
    c.commit()
    assert res2['created'] == 0 and res2['updated'] == 1
    assert res2['formula_ids'] == [fid]
    assert _inputs(c, fid) == {LOOSE: (1, ROLE_COMPONENT), OTHER_CARD: (1, ROLE_PACKAGING)}


def test_dedup_allows_bundle_to_plain_same_component(empty_db_conn):
    """The other legitimate in-place edit: dropping packaging_id (bundle ->
    plain pair) on the SAME component must still be an update, not a refusal."""
    c = empty_db_conn
    _seed_bundle_products(c)
    res1 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    fid = res1['formula_ids'][0]

    res2 = models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c)
    c.commit()
    assert res2['created'] == 0 and res2['updated'] == 1
    assert res2['formula_ids'] == [fid]
    assert _inputs(c, fid) == {LOOSE: (1, None)}


def test_route_rejects_repoint_to_different_loose_reshows_not_500(admin_client, tmp_db):
    pack_pid, loose_pid, other_loose_pid = 900461, 900462, 900463
    _seed_route_products(tmp_db, pack_pid, loose_pid, 900464)
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                (other_loose_pid, f"ROTHERLOOSE{other_loose_pid}", "อัน"))
    conn.commit()
    conn.close()
    import models as _m
    res0 = _m.upsert_pack_unpack_pair(pack_pid, loose_pid, 1, 'pack')
    fid_before = res0['formula_ids'][0]

    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(other_loose_pid), 'packaging_id': '',
        'ratio': '1', 'direction': 'pack',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]            # reshow, never a 500

    conn = sqlite3.connect(tmp_db)
    roles = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, role FROM conversion_formula_inputs WHERE formula_id=?", (fid_before,))}
    conn.close()
    assert roles == {loose_pid: None}                          # untouched — never rewired


# ── cross_unit_hazard — bundle-aware ────────────────────────────────────────

def test_cross_unit_hazard_bundle_pack_side_partner_is_component(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    hz = models.cross_unit_hazard(c, PACK, 'อัน')
    assert hz is not None
    assert hz['kind'] == 'pair'
    assert hz['partner_id'] == LOOSE                          # the component, never the card


def test_cross_unit_hazard_bundle_loose_side_partner_is_pack(empty_db_conn):
    c = empty_db_conn
    _seed_bundle_products(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    hz = models.cross_unit_hazard(c, LOOSE, 'แผง')
    assert hz is not None
    assert hz['kind'] == 'pair'
    assert hz['partner_id'] == PACK


def test_cross_unit_hazard_bundle_reverse_row_order_still_finds_component(empty_db_conn):
    """Row order must never carry meaning here either (same proof as
    test_bundle_input_rows_reverse_order_still_derives_same_component, but
    for cross_unit_hazard's OWN partner lookup — upsert_pack_unpack_pair
    always inserts loose-before-packaging, so that helper alone cannot
    prove this direction is order-independent). Build the formula by hand
    with the packaging row inserted FIRST, both directions."""
    c = empty_db_conn
    _seed_bundle_products(c)
    fid = c.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        ("[แพ็ค] hammer pack ⟵ 1 อัน + blister card", PACK, 1)).lastrowid
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
              (fid, CARD, 1, ROLE_PACKAGING))                # packaging inserted FIRST
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,?)",
              (fid, LOOSE, 1, ROLE_COMPONENT))
    c.commit()

    hz_pack = models.cross_unit_hazard(c, PACK, 'อัน')
    assert hz_pack is not None and hz_pack['kind'] == 'pair' and hz_pack['partner_id'] == LOOSE

    hz_loose = models.cross_unit_hazard(c, LOOSE, 'แผง')
    assert hz_loose is not None and hz_loose['kind'] == 'pair' and hz_loose['partner_id'] == PACK


def test_cross_unit_hazard_packaging_side_never_reports_pair(empty_db_conn):
    """The card plays 'packaging', not 'component' — it must never itself
    resolve to a 'pair' hazard via this bundle (that relationship belongs to
    PACK <-> LOOSE only). With CARD's own unit_type ('แผง') matching the
    bundle's OUTPUT unit, a naive 'any multi-input membership counts'
    implementation would report a false 'pair' with partner=PACK the moment
    CARD is checked in any OTHER unit — this is the shape that actually
    distinguishes role-aware from role-blind (querying CARD in its own unit
    'แผง' would short-circuit on the same-unit check before reaching this
    logic at all, so it proves nothing)."""
    c = empty_db_conn
    _seed_bundle_products(c)
    models.upsert_pack_unpack_pair(PACK, LOOSE, 1, direction='pack', conn=c, packaging_id=CARD)
    c.commit()
    hz = models.cross_unit_hazard(c, CARD, 'อัน')
    assert hz is None or hz['kind'] != 'pair'


def test_cross_unit_hazard_packaging_side_no_false_pair_even_when_units_align(empty_db_conn):
    """The distinguishing case for the direction-2 role check: CARD2's own
    unit ('ชิ้น') differs from the bundle output's unit ('แผง'), so the
    same-unit short-circuit does NOT apply — and PACK2 (the wrongly-guessed
    partner under a role-blind 'any multi-input membership' bug) DOES share
    its unit with the queried bsn_unit. A role-blind implementation reports
    a false 'pair' here; the role-aware one must not, because CARD2 plays
    'packaging', not 'component'."""
    c = empty_db_conn
    pack2, loose2, card2 = 111, 112, 113
    _seed_product(c, pack2, "pack2", "แผง")
    _seed_product(c, loose2, "loose2", "อัน")
    _seed_product(c, card2, "card2", "ชิ้น")
    c.commit()
    models.upsert_pack_unpack_pair(pack2, loose2, 1, direction='pack', conn=c, packaging_id=card2)
    c.commit()
    hz = models.cross_unit_hazard(c, card2, 'แผง')             # PACK2's own unit — the trap
    assert hz is None


def test_cross_unit_hazard_fails_closed_on_roleless_multi_input(empty_db_conn):
    """Break-it-once target for the 'never guess' rule: a hand-corrupted
    active [แพ็ค] formula with 2 inputs and NO roles must raise, never fall
    back to guessing a partner by row position."""
    c = empty_db_conn
    _seed_bundle_products(c)
    fid = c.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,?)",
        ("[แพ็ค] hammer pack ⟵ 1 อัน + blister card", PACK, 1)).lastrowid
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,NULL)",
              (fid, LOOSE, 1))
    c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role) VALUES (?,?,?,NULL)",
              (fid, CARD, 1))
    c.commit()
    with pytest.raises(ConversionRoleError):
        models.cross_unit_hazard(c, PACK, 'อัน')


def test_cross_unit_hazard_unaffected_for_plain_single_input_pairs(empty_db_conn):
    """Regression guard: the existing single-input pair guard (pinned in
    tests/test_unit_mapping_guard.py) must be untouched by the role-aware
    branch — a plain pair never even reaches component_product_id."""
    c = empty_db_conn
    _seed_product(c, PACK, "pack", "แผง")
    _seed_product(c, LOOSE, "loose", "ตัว")
    c.commit()
    models.upsert_pack_unpack_pair(PACK, LOOSE, 2, direction='both', conn=c)
    c.commit()
    hz = models.cross_unit_hazard(c, PACK, 'ตัว')
    assert hz is not None and hz['kind'] == 'pair' and hz['partner_id'] == LOOSE


# ── /conversions/pair route — packaging picker + reshow-on-error ───────────

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _seed_route_products(tmp_db, pack_pid, loose_pid, card_pid):
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                 (pack_pid, f"RPACK{pack_pid}", "แผง"))
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                 (loose_pid, f"RLOOSE{loose_pid}", "อัน"))
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                 (card_pid, f"RCARD{card_pid}", "แผง"))
    conn.commit()
    conn.close()


def test_route_post_with_packaging_creates_bundle_no_500(admin_client, tmp_db):
    pack_pid, loose_pid, card_pid = 900401, 900402, 900403
    _seed_route_products(tmp_db, pack_pid, loose_pid, card_pid)
    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(loose_pid), 'packaging_id': str(card_pid),
        'ratio': '1', 'direction': 'both', 'note': 'route-bundle-test',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    formulas = conn.execute(
        "SELECT id FROM conversion_formulas WHERE output_product_id=? AND is_active=1", (pack_pid,)
    ).fetchall()
    assert len(formulas) == 1
    fid = formulas[0][0]
    roles = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, role FROM conversion_formula_inputs WHERE formula_id=?", (fid,))}
    conn.close()
    assert roles == {loose_pid: ROLE_COMPONENT, card_pid: ROLE_PACKAGING}


def test_route_rejects_packaging_same_as_pack_reshows_not_500(admin_client, tmp_db):
    pack_pid, loose_pid = 900411, 900412
    _seed_route_products(tmp_db, pack_pid, loose_pid, 900413)  # card seeded but unused —
    # the POST below deliberately points packaging_id at pack_pid itself
    before = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1").fetchone()[0]

    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(loose_pid), 'packaging_id': str(pack_pid),
        'ratio': '1', 'direction': 'both',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]           # reshow, never a 500

    after = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1").fetchone()[0]
    assert after == before                                     # nothing written on a rejected save


def test_route_switch_existing_pair_to_bundle_no_500_no_duplicate(admin_client, tmp_db):
    """The exact failure this task exists to prevent at the web boundary: an
    operator adding a packaging component to an output that already has an
    active [แพ็ค] must not hit a raw IntegrityError / 500.

    302 + 'count still 1' is NOT enough evidence on its own — a route that
    silently did nothing (bailed at some guard, swallowed an exception) would
    ALSO redirect 302 and leave count at 1, since a plain-pair formula for
    pack_pid already exists before the POST (see 'A 302 from a Sendy route is
    not evidence the write happened', erp-engineering-discipline.md). The
    positive signal that could only come from the update actually running is
    the ROLE assignment on the formula's inputs."""
    pack_pid, loose_pid, card_pid = 900421, 900422, 900423
    _seed_route_products(tmp_db, pack_pid, loose_pid, card_pid)
    import models as _m
    res0 = _m.upsert_pack_unpack_pair(pack_pid, loose_pid, 1, 'pack')
    fid_before = res0['formula_ids'][0]

    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(loose_pid), 'packaging_id': str(card_pid),
        'ratio': '1', 'direction': 'both',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT id FROM conversion_formulas WHERE output_product_id=? AND is_active=1", (pack_pid,)
    ).fetchall()
    assert len(rows) == 1
    fid_after = rows[0][0]
    assert fid_after == fid_before                            # same formula, updated in place
    roles = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, role FROM conversion_formula_inputs WHERE formula_id=?", (fid_after,))}
    conn.close()
    # ONLY true if the bundle write actually ran — the pre-existing plain pair
    # had a single role=NULL input, not this role-tagged pair.
    assert roles == {loose_pid: ROLE_COMPONENT, card_pid: ROLE_PACKAGING}


def test_route_edit_prefills_packaging_field(admin_client, tmp_db):
    pack_pid, loose_pid, card_pid = 900431, 900432, 900433
    _seed_route_products(tmp_db, pack_pid, loose_pid, card_pid)
    import models as _m
    res = _m.upsert_pack_unpack_pair(pack_pid, loose_pid, 1, 'pack', packaging_id=card_pid)
    fid = res['formula_ids'][0]

    resp = admin_client.get(f'/conversions/pair?from_formula={fid}')
    assert resp.status_code == 200, resp.data[:500]
    body = resp.data.decode('utf-8', 'replace')
    assert f'RCARD{card_pid}' in body                         # packaging name prefilled
    assert f'value="{card_pid}"' in body                      # hidden packaging_id prefilled


def test_route_nonnumeric_packaging_id_reshows_not_500(admin_client, tmp_db):
    """Codex review finding 4 (blocker on PR #388): a hidden field carrying a
    non-numeric value (stale tab, hand-edited form) is not a trust boundary —
    the route must reshow with a flash, never 500 on a bare ValueError."""
    pack_pid, loose_pid = 900451, 900452
    _seed_route_products(tmp_db, pack_pid, loose_pid, 900453)
    before = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1").fetchone()[0]

    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(loose_pid), 'packaging_id': 'abc',
        'ratio': '1', 'direction': 'both',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]            # reshow, never a 500

    after = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE is_active=1").fetchone()[0]
    assert after == before                                     # nothing written


def test_route_nonnumeric_pack_id_reshows_not_500(admin_client, tmp_db):
    """Same trust-boundary gap on pack_id itself (the first int() the route
    calls, previously outside the try/except that catches ConversionRoleError
    / IntegrityError)."""
    resp = admin_client.post('/conversions/pair', data={
        'pack_id': 'abc', 'loose_id': '1', 'packaging_id': '',
        'ratio': '1', 'direction': 'both',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]


def test_route_no_packaging_still_creates_plain_pair(admin_client, tmp_db):
    """Regression control: leaving the new field blank must behave exactly
    as before (pinned already by test_conversion_routes.py, repeated here
    with the packaging_id field present-but-empty to prove the new code path
    doesn't kick in on a blank optional field)."""
    pack_pid, loose_pid = 900441, 900442
    _seed_route_products(tmp_db, pack_pid, loose_pid, 900443)
    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_pid), 'loose_id': str(loose_pid), 'packaging_id': '',
        'ratio': '2', 'direction': 'both',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    formulas = conn.execute(
        "SELECT id, output_qty FROM conversion_formulas WHERE output_product_id=? AND is_active=1",
        (pack_pid,)).fetchall()
    assert len(formulas) == 1
    roles = [r[0] for r in conn.execute(
        "SELECT role FROM conversion_formula_inputs WHERE formula_id=?", (formulas[0][0],))]
    conn.close()
    assert roles == [None]
    # both halves exist (plain pair, direction='both')
    n_unpack = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE output_product_id=? AND is_active=1", (loose_pid,)
    ).fetchone()[0]
    assert n_unpack == 1
