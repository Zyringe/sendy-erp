"""BSN import must never touch platform_skus.stock (order-driven-platform-
deduction plan, Phase 1 cutover: task 1.4 retired the walk, task 1.6 rewrote
this file).

Historically `_sync_bsn_to_stock` walked a product's listings and deducted or
credited `platform_skus.stock` whenever a หน้าร้าน sale/return first synced —
see `projects/platform-deduction-provenance/plan.md` (mig 172, PR #425) for
the mechanism this file used to pin. Put's ruling (2026-08-25): stock should
be deducted only when a marketplace ORDER file is imported, so the operator
knows exactly WHICH listing is wrong. The walk is now DELETED from
`_sync_bsn_to_stock`; the mirror is maintained solely by
`import_marketplace_orders`'s diff engine (see
tests/test_order_driven_deduction.py — that file, not this one, now owns
"does a marketplace event deduct/credit the right listing").

This file pins two things:
  1. BSN sales/purchase imports — first sync, a second sale, an unchanged
     reimport, an oversized sale, a customer return, a NULL-stock listing —
     never write to platform_skus.stock and never create a
     platform_stock_deductions row. The warehouse ledger (stock_levels) is
     the only thing they still move; every test below carries a CONTROL
     proving that ledger posting is untouched, not merely "nothing ran".
  2. `reverse_platform_deduction` and its two call sites in imports.py
     (corrected/removed lines) are UNCHANGED code (D7 in the plan) and still
     have to behave correctly against a PRE-CUTOVER row that already holds a
     provenance record. Since BSN import can no longer CREATE that record,
     these tests seed it directly (`_seed_provenance` + `_set_platform_stock`)
     to simulate "a row the retired walk had already deducted", then exercise
     the correction/removal/refresh/manual-edit path exactly as the walk-era
     tests did.

Measured on prod 2026-08-24, the failure this file originally existed to pin:
a BSN bill import replayed a product's whole marketplace history and drove
six live listings to 0 (บานพับ #412 from 501 against 1,967 lifetime units).
That regression is now structurally impossible for NEW rows — there is no
walk left to replay.
"""
import os
import sqlite3

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")
_MIG_172 = os.path.join(_REPO, "data", "migrations",
                        "172_platform_stock_deductions.sql")

PLATFORM_CUSTOMER = 'หน้าร้านS'      # PLATFORM_STOCK_DEDUCT_CUSTOMERS -> shopee


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _ensure_bsn_unit(c):
    """empty_db clones the live schema, which may predate mig 124 on this
    machine — apply it once so import_weekly's _resolve_mapping call does not
    hit 'no such column: bsn_unit'."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(product_code_mapping)")}
    if "bsn_unit" not in cols:
        with open(_MIG_124, encoding="utf-8") as f:
            c.executescript(f.read())


def _ensure_deduction_provenance(c):
    """empty_db clones the live schema, which does not carry mig 172 until it
    is merged and the dev DB has been booted against it — and deliberately so:
    applying an unmerged migration to the shared dev DB leaks it to every other
    worktree on this machine. Apply it to the TEST database instead, the same
    way _ensure_bsn_unit handles mig 124."""
    have = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='platform_stock_deductions'").fetchone()[0]
    if not have:
        with open(_MIG_172, encoding="utf-8") as f:
            c.executescript(f.read())


def _seed(path, code, listing_stock, unit_type='ตัว'):
    """A product mapped to `code`, plus one Shopee listing holding
    `listing_stock` units. Everything the test asserts is forced here rather
    than inherited: empty_db clones the live SCHEMA but the rows are ours."""
    c = _conn(path)
    _ensure_bsn_unit(c)
    _ensure_deduction_provenance(c)
    pid = c.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?,?,0)",
        (f"P{code}", unit_type)).lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?,0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES (?,?,?)", (code, f"n{code}", pid))
    c.execute(
        "INSERT INTO platform_skus (platform, product_name, variation_id,"
        " internal_product_id, qty_per_sale, stock)"
        " VALUES ('shopee', ?, ?, ?, 1, ?)",
        (f"listing {code}", f"V{code}", pid, listing_stock))
    c.commit()
    c.close()
    return pid


def _entry(doc_no, code, qty, *, unit='ตัว', price=10.0, party=PLATFORM_CUSTOMER):
    return {
        'date_iso': '2026-08-24', 'doc_no': doc_no, 'line_seq': 1,
        'qty': qty, 'unit': unit, 'unit_price': price,
        'vat_type': 0, 'discount': '', 'total': qty * price, 'net': qty * price,
        'product_name_raw': 'n', 'product_code_raw': code,
        'party': party, 'party_code': 'pc',
    }


def _platform_stock(path, pid):
    c = _conn(path)
    row = c.execute(
        "SELECT stock FROM platform_skus WHERE internal_product_id=?", (pid,)).fetchone()
    c.close()
    return None if row is None else row['stock']


def _stock(path, pid):
    """stock_levels read directly — an independent signal from the platform
    column, so a test can tell 'the warehouse ledger is fine, only marketplace
    stock is wrong' apart from 'everything is wrong'."""
    c = _conn(path)
    row = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    c.close()
    return None if row is None else row['quantity']


def _provenance(path, pid):
    c = _conn(path)
    rows = c.execute(
        "SELECT d.units FROM platform_stock_deductions d"
        " JOIN platform_skus s ON s.id = d.platform_sku_id"
        " WHERE s.internal_product_id = ?", (pid,)).fetchall()
    c.close()
    return [r['units'] for r in rows]


def _sku_id(path, pid):
    """The (single, per `_seed`) platform_skus.id for a seeded product."""
    c = _conn(path)
    row = c.execute(
        "SELECT id FROM platform_skus WHERE internal_product_id=?", (pid,)).fetchone()
    c.close()
    return row['id']


def _source_row_id(path, table, doc_no, code=None):
    """The stored source-row id for a doc — `code` disambiguates when a doc
    carries more than one line (BSN sales doc_no is not unique per line)."""
    c = _conn(path)
    sql = f"SELECT id FROM {table} WHERE doc_no=?"
    params = [doc_no]
    if code is not None:
        sql += " AND bsn_code=?"
        params.append(code)
    row = c.execute(sql, params).fetchone()
    c.close()
    return row['id']


def _seed_provenance(path, sku_id, source_table, source_id, units):
    """Directly insert a `platform_stock_deductions` row — simulating a
    PRE-CUTOVER row the retired walk had already deducted/credited. BSN import
    itself never creates these anymore, so any test that needs
    `reverse_platform_deduction` to have something to reverse must seed it
    this way (D7)."""
    c = _conn(path)
    _ensure_deduction_provenance(c)
    c.execute(
        "INSERT INTO platform_stock_deductions"
        " (source_table, source_id, platform_sku_id, units) VALUES (?,?,?,?)",
        (source_table, source_id, sku_id, units))
    c.commit()
    c.close()


def _set_platform_stock(path, pid, stock):
    """Directly set a seeded product's listing stock — the other half of
    simulating a pre-cutover state, since inserting provenance alone does not
    move the figure it describes."""
    c = _conn(path)
    c.execute("UPDATE platform_skus SET stock=? WHERE internal_product_id=?", (stock, pid))
    c.commit()
    c.close()


# ── BSN import leaves platform_skus.stock alone ─────────────────────────────
#
# Bucket 1+4 of the task-1.6 disposition: every test here used to assert that
# a BSN import (sale or return) DEDUCTED or CREDITED platform_skus.stock via
# the walk. Phase 1 retires that walk entirely, so each is inverted: the
# mirror must stay exactly where it started, and the warehouse ledger CONTROL
# proves the import still did real work.

def test_bsn_import_leaves_platform_stock_alone(empty_db):
    """The Phase-1 cutover, across the shapes that used to matter to the
    walk: a first-sync sale, a second sale on the same product (which also
    forces pass 2 to rebuild the first row alongside the new one), and an
    unchanged reimport. None may touch platform_skus.stock."""
    import models
    pid = _seed(empty_db, 'B900', listing_stock=100)

    models.import_weekly([_entry('IV001', 'B900', 5)], 'sales', 'week1')
    assert _platform_stock(empty_db, pid) == 100, 'first sync must not deduct'
    assert _stock(empty_db, pid) == -5, 'CONTROL: warehouse OUT still posts'

    models.import_weekly([_entry('IV002', 'B900', 1)], 'sales', 'week2')
    assert _stock(empty_db, pid) == -6, 'CONTROL: warehouse ledger counts both sales'
    assert _platform_stock(empty_db, pid) == 100, 'second import must not deduct either'

    stats = models.import_weekly([_entry('IV002', 'B900', 1)], 'sales', 'week2-again')
    assert stats['unchanged'] == 1, stats
    assert stats['affected_products'] == 0, stats
    assert _platform_stock(empty_db, pid) == 100
    assert _provenance(empty_db, pid) == [], 'no provenance is ever created'


def test_a_pass_2_replay_does_not_error_and_leaves_platform_stock_alone(empty_db):
    """Pass 2 resets synced_to_stock=0 for every row of an affected product
    and re-posts through _sync_bsn_to_stock — including rows that were
    already synced before this import. The retired walk special-cased this
    via replayed_ids to avoid double-deducting; there is nothing left to
    guard now, but pass 2 must still run cleanly (no exception) and leave
    platform_skus.stock untouched (task-1.6 disposition: the ONE test kept
    from the replay-protection bucket)."""
    import models
    pid = _seed(empty_db, 'B971', listing_stock=100)

    models.import_weekly([_entry('IV930', 'B971', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 100          # CONTROL

    # A second, unrelated sale of the SAME product forces pass 2 to reset and
    # replay IV930 alongside the new IV931 line.
    models.import_weekly([_entry('IV931', 'B971', 2)], 'sales', 'w2')

    assert _stock(empty_db, pid) == -7, 'CONTROL: warehouse ledger counts both sales'
    assert _platform_stock(empty_db, pid) == 100, 'pass-2 replay must not deduct'
    assert _provenance(empty_db, pid) == []


def test_bsn_import_leaves_platform_stock_alone_even_when_the_sale_exceeds_it(empty_db):
    """The retired walk used to clamp at MAX(0, stock - n) when a sale
    exceeded the listing's stock. BSN import creates no deduction at all now,
    so an oversized sale must not even approach that clamp."""
    import models
    pid = _seed(empty_db, 'B970', listing_stock=3)

    models.import_weekly([_entry('IV920', 'B970', 10)], 'sales', 'w1')

    assert _stock(empty_db, pid) == -10, 'CONTROL: the warehouse ledger still moves'
    assert _platform_stock(empty_db, pid) == 3, 'the listing is untouched, not clamped to 0'
    assert _provenance(empty_db, pid) == []


def test_bsn_import_of_a_customer_return_also_leaves_platform_stock_alone(empty_db):
    """SR rows no longer touch the mirror either (D4 in the plan): a return
    still moves the WAREHOUSE ledger (goods come back), but the
    platform-stock credit now comes from the marketplace order's status
    transition (task 1.3's diff engine), not from this BSN walk."""
    import models
    pid = _seed(empty_db, 'C100', listing_stock=100)

    models.import_weekly([_entry('IV800', 'C100', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 100          # CONTROL

    models.import_weekly([_entry('SR800', 'C100', 5)], 'sales', 'w2')

    assert _stock(empty_db, pid) == 0, 'the warehouse ledger still takes the goods back in'
    assert _platform_stock(empty_db, pid) == 100, 'the mirror is untouched by BSN import'
    assert _provenance(empty_db, pid) == [], 'no provenance — the walk is gone'


def test_bsn_import_leaves_a_null_stock_listing_alone(empty_db):
    """`platform_skus.stock` is nullable (two live Lazada listings sit that
    way today). BSN import never writes to platform_skus.stock at all now, so
    a NULL listing stays NULL regardless of whether the BSN row is a sale or
    a return."""
    import models
    pid = _seed(empty_db, 'B996', listing_stock=100)
    c = _conn(empty_db)
    c.execute("UPDATE platform_skus SET stock=NULL WHERE internal_product_id=?", (pid,))
    c.commit()
    c.close()

    models.import_weekly([_entry('IV970', 'B996', 5)], 'sales', 'w1')
    assert _stock(empty_db, pid) == -5, 'CONTROL: the warehouse ledger still moves'
    assert _platform_stock(empty_db, pid) is None
    assert _provenance(empty_db, pid) == []

    models.import_weekly([_entry('SR970', 'B996', 5)], 'sales', 'w2')
    assert _stock(empty_db, pid) == 0, 'CONTROL: the warehouse ledger still moves'
    assert _platform_stock(empty_db, pid) is None
    assert _provenance(empty_db, pid) == []


# ── reverse_platform_deduction on a PRE-CUTOVER row (D7, unchanged code) ────
#
# `reverse_platform_deduction` and its imports.py call sites (corrected /
# removed lines) are untouched by Phase 1 — they exist to undo whatever a
# source row did to marketplace stock, and that is still real work for a row
# the retired walk deducted before the cutover. BSN import can no longer
# CREATE that provenance, so every test here seeds it directly via
# `_seed_provenance` + `_set_platform_stock` to simulate exactly that row,
# then drives the same correction/removal/refresh/manual-edit path the
# walk-era tests did.

def test_a_corrected_line_reverses_the_historical_deduction_without_rededucting(empty_db):
    """A pre-cutover row that already holds provenance (the retired walk once
    deducted it) gets corrected. imports.py's correction path still calls
    reverse_platform_deduction on the deleted old row (D7) — so the
    historical 5 units come back — but the REPLACEMENT row created by the
    correction is a plain post-cutover BSN row and never deducts anything:
    platform stock lands at 100, not at "95 minus the corrected amount" the
    old walk would have produced.
    """
    import models
    pid = _seed(empty_db, 'B950', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('IV900', 'B950', 5)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'IV900')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, 5)
    _set_platform_stock(empty_db, pid, 95)
    assert _platform_stock(empty_db, pid) == 95    # CONTROL: simulated pre-cutover state

    stats = models.import_weekly([_entry('IV900', 'B950', 7)], 'sales', 'w2')

    # CONTROL — prove the correction really was processed as a change.
    assert stats['overwritten'] == 1, stats
    assert _stock(empty_db, pid) == -7, 'the warehouse ledger IS exact'
    assert _platform_stock(empty_db, pid) == 100, (
        'the historical deduction is reversed; the replacement never deducts')
    assert _provenance(empty_db, pid) == [], 'no new provenance — the walk is gone'


def test_a_removed_line_reverses_its_historical_deduction(empty_db):
    """A pre-cutover row (simulated via direct provenance) whose sales line is
    later removed from the source file. imports.py's removal path still calls
    reverse_platform_deduction (D7) so the historical units come back — this
    code path did not change, only how the provenance it reverses gets
    created."""
    import models
    pid = _seed(empty_db, 'B960', listing_stock=100)
    other = _seed(empty_db, 'B961', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    # One document, two lines: ours plus a second line that keeps the document
    # present in next week's file (removal detection is scoped to doc_base the
    # file mentions, so a doc that vanishes entirely is never reversed).
    models.import_weekly([_entry('IV910', 'B960', 5),
                          _entry('IV910', 'B961', 1)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'IV910', code='B960')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, 5)
    _set_platform_stock(empty_db, pid, 95)
    assert _platform_stock(empty_db, pid) == 95     # CONTROL: simulated pre-cutover
    assert _platform_stock(empty_db, other) == 100  # CONTROL: post-cutover row, untouched

    stats = models.import_weekly([_entry('IV910', 'B961', 1)], 'sales', 'w2')

    # CONTROL — the line really was detected as removed.
    assert stats['removed'] == 1, stats
    assert _stock(empty_db, pid) == 0, 'the warehouse ledger DID reverse it'
    assert _platform_stock(empty_db, pid) == 100, (
        'a removed sale must return the historical units it took off the listing')


def test_a_platform_refresh_supersedes_the_historical_record(empty_db):
    """import_platform_skus's invalidation (unchanged code) still supersedes
    a RECORDED historical deduction — simulated directly since BSN import no
    longer creates any. 100 -> (simulated) sale of 5 -> 95 -> file says 92 ->
    remove the sale. Without the invalidation this would land on 97 while the
    platform holds 92 — the oversell direction.
    """
    import models
    pid = _seed(empty_db, 'B980', listing_stock=100)
    keeper = _seed(empty_db, 'B981', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('IV940', 'B980', 5),
                          _entry('IV940', 'B981', 1)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'IV940', code='B980')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, 5)
    _set_platform_stock(empty_db, pid, 95)
    assert _provenance(empty_db, pid) == [5]              # CONTROL
    assert _platform_stock(empty_db, keeper) == 100        # CONTROL: never deducted

    # An authoritative refresh: the Seller Center export says 92.
    models.import_platform_skus('shopee', [{
        'variation_id': 'VB980', 'product_id_str': 'p', 'product_name': 'LB980',
        'variation_name': None, 'parent_sku': None, 'seller_sku': None,
        'price': 10.0, 'special_price': None, 'stock': 92, 'raw_json': '{}',
    }])
    assert _platform_stock(empty_db, pid) == 92, 'CONTROL: the file value landed'
    assert _provenance(empty_db, pid) == [], 'the refresh supersedes the record'

    stats = models.import_weekly([_entry('IV940', 'B981', 1)], 'sales', 'w2')

    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 92, (
        'must stay at the platform figure, not 97 — the refresh already '
        'accounted for that historical sale')


def test_a_partial_export_only_supersedes_the_listings_it_carried(empty_db):
    """A Seller Center export can be category-filtered, and
    import_platform_skus upserts ONLY the rows it was given. A historical
    record for a listing the file did not mention must survive — wiping it
    would make a later correction or removal silently restore nothing.
    """
    import models
    mentioned = _seed(empty_db, 'B990', listing_stock=100)
    absent = _seed(empty_db, 'B991', listing_stock=100)
    m_sku = _sku_id(empty_db, mentioned)
    a_sku = _sku_id(empty_db, absent)

    models.import_weekly([_entry('IV950', 'B990', 5),
                          _entry('IV950', 'B991', 4)], 'sales', 'w1')
    m_id = _source_row_id(empty_db, 'sales_transactions', 'IV950', code='B990')
    a_id = _source_row_id(empty_db, 'sales_transactions', 'IV950', code='B991')
    _seed_provenance(empty_db, m_sku, 'sales_transactions', m_id, 5)
    _seed_provenance(empty_db, a_sku, 'sales_transactions', a_id, 4)
    _set_platform_stock(empty_db, mentioned, 95)
    _set_platform_stock(empty_db, absent, 96)
    assert _provenance(empty_db, mentioned) == [5]     # CONTROL
    assert _provenance(empty_db, absent) == [4]        # CONTROL

    # A file carrying ONLY B990's listing.
    models.import_platform_skus('shopee', [{
        'variation_id': 'VB990', 'product_id_str': 'p', 'product_name': 'LB990',
        'variation_name': None, 'parent_sku': None, 'seller_sku': None,
        'price': 10.0, 'special_price': None, 'stock': 90, 'raw_json': '{}',
    }])

    assert _provenance(empty_db, mentioned) == [], 'carried by the file -> superseded'
    assert _provenance(empty_db, absent) == [4], (
        'not in the file -> its stock was never refreshed, so the record stands')
    assert _platform_stock(empty_db, absent) == 96, 'CONTROL: untouched by the import'


def test_a_deleted_return_line_reverses_the_historical_credit(empty_db):
    """D7 symmetry for removed RETURN lines. Simulated pre-cutover state: a
    sale and its return, both already reflected historically (sale +5,
    return -5, net back to where the listing started). Removing the return
    line's source row must reverse ONLY its own record — recording credits
    unsigned would have left this one un-reversible, a fresh instance of the
    very bug this table exists to remove."""
    import models
    pid = _seed(empty_db, 'C101', listing_stock=100)
    keeper = _seed(empty_db, 'C102', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('IV810', 'C101', 5)], 'sales', 'w1')
    iv_id = _source_row_id(empty_db, 'sales_transactions', 'IV810')
    models.import_weekly([_entry('SR810', 'C101', 5),
                          _entry('SR810', 'C102', 1)], 'sales', 'w2')
    sr_id = _source_row_id(empty_db, 'sales_transactions', 'SR810', code='C101')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', iv_id, 5)
    _seed_provenance(empty_db, sku_id, 'sales_transactions', sr_id, -5)
    _set_platform_stock(empty_db, pid, 100)
    assert _platform_stock(empty_db, pid) == 100      # CONTROL: simulated pre-cutover
    assert _platform_stock(empty_db, keeper) == 100   # CONTROL: post-cutover, untouched

    stats = models.import_weekly([_entry('SR810', 'C102', 1)], 'sales', 'w3')

    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 95, (
        'the return never happened, so its historical credit comes back off')
    assert _provenance(empty_db, pid) == [5], 'only the original sale record remains'


def test_undoing_a_historical_return_cannot_drive_a_listing_negative(empty_db):
    """reverse_platform_deduction is unchanged (D7): undoing a RETURN still
    moves stock DOWN, and the credited units may already have been consumed
    by later activity. Simulated directly: a historical SR credit whose units
    have since been sold again (stock preset to reflect that), then the SR
    line is removed. The raw subtraction lands on -3; clamped it lands on 0.
    """
    import models
    pid = _seed(empty_db, 'C110', listing_stock=2)
    keeper = _seed(empty_db, 'C111', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('SR830', 'C110', 5),
                          _entry('SR830', 'C111', 1)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'SR830', code='C110')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, -5)
    assert _platform_stock(empty_db, pid) == 2       # CONTROL: simulated post-consumption state

    stats = models.import_weekly([_entry('SR830', 'C111', 1)], 'sales', 'w2')

    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 0, 'clamped, not -3'


def test_a_hand_typed_stock_figure_supersedes_the_historical_record(empty_db):
    """The other door into platform_skus.stock. `update_platform_sku` is the
    /ecommerce SKU edit form (unchanged code), and a hand-typed figure is
    authoritative for that listing exactly like a file is — so it has to
    supersede a historical record, or reversing it later invents units the
    operator's number already accounts for."""
    import models
    pid = _seed(empty_db, 'C130', listing_stock=100)
    other = _seed(empty_db, 'C131', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('IV850', 'C130', 5),
                          _entry('IV850', 'C131', 1)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'IV850', code='C130')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, 5)
    _set_platform_stock(empty_db, pid, 95)
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    models.update_platform_sku(sku_id, price=10.0, special_price=None,
                               stock=92, qty_per_sale=1)

    assert _provenance(empty_db, pid) == [], 'the typed figure supersedes it'

    stats = models.import_weekly([_entry('IV850', 'C131', 1)], 'sales', 'w2')
    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 92, 'stays at what the operator typed, not 97'


def test_a_price_only_edit_keeps_the_historical_record(empty_db):
    """The edit form resubmits every field, so a price-only save carries the
    unchanged stock with it. Discarding a live historical record there would
    make a later reversal restore nothing — the opposite error."""
    import models
    pid = _seed(empty_db, 'C140', listing_stock=100)
    other = _seed(empty_db, 'C141', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('IV860', 'C140', 5),
                          _entry('IV860', 'C141', 1)], 'sales', 'w1')
    old_id = _source_row_id(empty_db, 'sales_transactions', 'IV860', code='C140')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', old_id, 5)
    _set_platform_stock(empty_db, pid, 95)
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    models.update_platform_sku(sku_id, price=12.5, special_price=None,
                               stock=95, qty_per_sale=1)   # stock unchanged

    assert _provenance(empty_db, pid) == [5], 'nothing moved, so the record stands'

    stats = models.import_weekly([_entry('IV860', 'C141', 1)], 'sales', 'w2')
    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 100, 'the historical sale is properly reversed'


def test_a_price_only_correction_carries_over_the_historical_record(empty_db):
    """imports.py's carry_from logic is unchanged (D7): when a correction
    does not change the stock-affecting identity (price only), an existing
    record for the OLD row is carried over to the replacement rather than
    reversed. Simulated on a pre-cutover state: a historical return credit
    and a historical sale deduction, both seeded directly.

    Walked on the hostile state: a return credited 5, a sale later took 8,
    net 2. Correcting the SR's PRICE must leave 2 exactly and carry both
    records.
    """
    import models
    pid = _seed(empty_db, 'C150', listing_stock=2)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('SR850', 'C150', 5)], 'sales', 'w1')
    sr_id = _source_row_id(empty_db, 'sales_transactions', 'SR850')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', sr_id, -5)

    models.import_weekly([_entry('IV870', 'C150', 8)], 'sales', 'w2')
    iv_id = _source_row_id(empty_db, 'sales_transactions', 'IV870')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', iv_id, 8)
    assert _platform_stock(empty_db, pid) == 2         # CONTROL: simulated state

    stats = models.import_weekly([_entry('SR850', 'C150', 5, price=99.0)], 'sales', 'w3')

    assert stats['overwritten'] == 1, stats
    assert _platform_stock(empty_db, pid) == 2, 'a price fix is not a stock event'
    assert sorted(_provenance(empty_db, pid)) == [-5, 8], (
        'both historical records intact — the return carried to the '
        'replacement rather than being destroyed and remade, and the sale '
        'record untouched')


def test_a_quantity_correction_reverses_the_historical_credit_and_clamps(empty_db):
    """The residual, pinned. reverse_platform_deduction's clamp (D7,
    unchanged) applied to a simulated pre-cutover state: a historical SR
    credit whose units have already been consumed by later (untracked,
    post-cutover) activity, then the SR's quantity itself is corrected.

    The undo wants 2 - 5 = -3 and floors at 0. Unlike the walk era, the
    replacement row is a plain post-cutover BSN row and creates no new
    record, so the figure stays at the clamped 0 — nothing re-credits it,
    because there is no walk left to do that.
    """
    import models
    pid = _seed(empty_db, 'C160', listing_stock=2)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('SR860', 'C160', 5)], 'sales', 'w1')
    sr_id = _source_row_id(empty_db, 'sales_transactions', 'SR860')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', sr_id, -5)
    assert _platform_stock(empty_db, pid) == 2         # CONTROL

    stats = models.import_weekly([_entry('SR860', 'C160', 4)], 'sales', 'w2')

    assert stats['overwritten'] == 1, stats
    assert _platform_stock(empty_db, pid) == 0, 'clamped, and nothing re-credits it now'
    assert _provenance(empty_db, pid) == [], 'the historical record is consumed by the reversal'


def test_a_clamped_reversal_is_reported_not_swallowed(empty_db):
    """reverse_platform_deduction is unchanged (D7): when its undo clamps,
    the caller counts it as lossy so import_weekly can flash the operator.
    Pinned on a simulated pre-cutover state: a historical credit already
    partly consumed, then a quantity correction to that same row."""
    import models
    pid = _seed(empty_db, 'C170', listing_stock=2)
    keeper = _seed(empty_db, 'C171', listing_stock=100)
    sku_id = _sku_id(empty_db, pid)

    models.import_weekly([_entry('SR870', 'C170', 5)], 'sales', 'w1')
    sr_id = _source_row_id(empty_db, 'sales_transactions', 'SR870')
    _seed_provenance(empty_db, sku_id, 'sales_transactions', sr_id, -5)

    # CONTROL — an ordinary import (nothing corrected/removed) reports zero,
    # so the counter is not just always-on noise.
    clean = models.import_weekly([_entry('IV891', 'C171', 1)], 'sales', 'w2')
    assert clean['lossy_platform_reversals'] == 0, clean

    lossy = models.import_weekly([_entry('SR870', 'C170', 4)], 'sales', 'w3')

    assert lossy['overwritten'] == 1, lossy
    assert lossy['lossy_platform_reversals'] == 1, lossy


# ── The lossy-reversal warning helper — unaffected by the walk's retirement ─

def test_the_lossy_warning_is_one_helper_used_by_every_import_entry_point():
    """Cross-cutting sweep, left behind as a test per
    .claude/rules/erp-engineering-discipline.md. There are TWO routes that call
    import_weekly — the unified upload box and the Express DBF upload — and the
    first version of this warning was wired to only one of them (Codex,
    2026-08-25). Both now go through `_lossy_reversal_warning`, and the stats
    key is read in exactly one place so a third entry point cannot quietly
    skip the signal.
    """
    from blueprints import bsn as bsn_bp

    src = open(bsn_bp.__file__, encoding='utf-8').read()
    # CONTROL — prove the file really is the one that wires the import routes,
    # so a mis-resolved path cannot make the counts below vacuously pass.
    assert 'import_router.commit_file(' in src
    assert 'import_router.commit_express_dbf(' in src

    assert src.count('lossy_platform_reversals') == 1, (
        'the stats key must be read only inside the helper')
    assert src.count('_lossy_reversal_warning(') == 3, (
        'one definition + one call per import_weekly entry point')


def test_the_lossy_warning_says_may_not_is():
    """A clamped undo does NOT prove the figure ended up high: in the
    5 -> return +5 -> sale -8 -> delete sequence the removal lands on 0, and so
    does a replay without the return, because the sale floors too. Asserting
    'the figure is high' would make the rare warning a demonstrable false
    positive (Codex, 2026-08-25)."""
    from blueprints.bsn import _lossy_reversal_warning

    assert _lossy_reversal_warning(None, 'f') is None
    assert _lossy_reversal_warning({}, 'f') is None
    assert _lossy_reversal_warning({'lossy_platform_reversals': 0}, 'f') is None

    msg = _lossy_reversal_warning({'lossy_platform_reversals': 2}, 'week.xlsx')
    assert msg is not None
    assert 'week.xlsx' in msg and '2' in msg
    assert 'อาจคลาดเคลื่อน' in msg, 'hedged, not asserted'
