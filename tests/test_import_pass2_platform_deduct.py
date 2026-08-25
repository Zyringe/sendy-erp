"""import_weekly pass 2 must not re-deduct platform_skus.stock.

`_sync_bsn_to_stock`'s docstring already names this hazard and ships a
`deduct_platform=False` guard for it: the platform deduction belongs to a row
FIRST becoming synced (an import), it is NOT reversed when the ledger row is
deleted, so replaying a product's whole ledger walks marketplace stock down by
its entire sales history every time.

Two of the three replay call sites pass the guard (`repoint_bsn_code`,
`update_unit_conversion_ratio`). `import_weekly`'s pass 2 — which performs the
exact same motion (delete the BSN ledger rows, reset synced_to_stock=0 for
EVERY row of the affected products, re-post) — did not, and it is the call site
that runs every week.

Measured on prod 2026-08-24: a marketplace file import at 16:04:30 stored the
platform's real numbers, a BSN bill import at 16:49:56 then replayed each
affected product's whole history, and six live listings were driven to 0 —
บานพับ #412 from 501 (its lifetime marketplace sales are 1,967, so MAX(0, ...)
swallowed the overshoot silently). /ecommerce then showed six false 🔴
"out of stock on the marketplace while Sendy has stock" rows.

The distinction the fix has to keep: a row that is NEW in this import is a
first sync and DOES owe its deduction. Blanket-disabling the deduction for
pass 2 would leave marketplace stock too HIGH instead — the oversell direction.
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


def test_a_later_import_does_not_re_deduct_an_earlier_marketplace_sale(empty_db):
    """The regression itself: two imports, two separate marketplace sales.

    Pass 2 of the SECOND import resets synced_to_stock=0 for every row of the
    affected product and re-posts both. Only the newly-arrived sale may touch
    platform_skus.stock; the first sale was already taken off during its own
    import and deleting its ledger row did not give it back.
    """
    import models
    pid = _seed(empty_db, 'B900', listing_stock=100)

    models.import_weekly([_entry('IV001', 'B900', 5)], 'sales', 'week1')
    # CONTROL — the first-sync deduction is correct and must still happen.
    # Without this the test could pass on a build where NOTHING ever deducts.
    assert _platform_stock(empty_db, pid) == 95
    assert _stock(empty_db, pid) == -5

    models.import_weekly([_entry('IV002', 'B900', 1)], 'sales', 'week2')

    assert _stock(empty_db, pid) == -6, 'warehouse ledger must count both sales'
    assert _platform_stock(empty_db, pid) == 94, (
        'pass 2 re-deducted the earlier sale: 95 - 5 - 1 = 89 instead of 95 - 1')


def test_repeated_imports_do_not_compound_the_over_deduction(empty_db):
    """The prod shape: one big historical sale, then several small weekly
    imports. Each replay re-applies the whole history, so the listing reaches
    the MAX(0, ...) clamp and the overshoot becomes invisible."""
    import models
    pid = _seed(empty_db, 'B901', listing_stock=60)

    models.import_weekly([_entry('IV101', 'B901', 50)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 10          # CONTROL

    for i, doc in enumerate(('IV102', 'IV103'), start=1):
        models.import_weekly([_entry(doc, 'B901', 1)], 'sales', f'w{i + 1}')

    assert _stock(empty_db, pid) == -52
    assert _platform_stock(empty_db, pid) == 8, (
        'each replay re-applied the 50-unit history and clamped the listing to 0')


def test_an_unchanged_reimport_leaves_platform_stock_alone(empty_db):
    """Re-uploading the same file changes nothing, so pass 2 must not run at
    all — and platform stock must not move. Pins the no-op path the diff-based
    importer promises."""
    import models
    pid = _seed(empty_db, 'B902', listing_stock=40)

    models.import_weekly([_entry('IV201', 'B902', 4)], 'sales', 'first')
    assert _platform_stock(empty_db, pid) == 36          # CONTROL

    stats = models.import_weekly([_entry('IV201', 'B902', 4)], 'sales', 'again')

    assert stats['unchanged'] == 1, stats
    assert stats['affected_products'] == 0, stats
    assert _platform_stock(empty_db, pid) == 36


# ── Corrected and removed lines: now EXACT ──────────────────────────────────
#
# These two were shipped in PR #424 pinned at the wrong-but-bounded value the
# code then produced (88 and 95), with the exact answer named in the assertion
# message and a note that they were expected to change once per-row deduction
# accounting landed. mig 172 + platform_stock_deductions is that accounting, so
# they now assert the exact values. Changing them was the plan, not a
# regression — see projects/platform-deduction-provenance/plan.md.

def test_a_corrected_line_deducts_only_the_delta(empty_db):
    """A sale corrected 5 -> 7. Pass 1 DELETEs the old source row and INSERTs a
    replacement with a fresh id, so the replacement is not in replayed_ids and
    deducts its full 7 on top of the 5 already taken.

    Pass 1 now REVERSES the old row's recorded deduction before deleting it, so
    the 5 goes back on and the replacement takes its full 7 — net 93, the delta.
    (Suppressing the replacement instead would give 95, under-deducting by the
    delta; that is why the fix is reversal and not a business-key skip.)
    """
    import models
    pid = _seed(empty_db, 'B950', listing_stock=100)

    models.import_weekly([_entry('IV900', 'B950', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95          # CONTROL

    stats = models.import_weekly([_entry('IV900', 'B950', 7)], 'sales', 'w2')

    # CONTROL — prove the correction really was processed as a change, not
    # silently treated as unchanged (which would make the assertion below pass
    # for the wrong reason).
    assert stats['overwritten'] == 1, stats
    assert _stock(empty_db, pid) == -7, 'the warehouse ledger IS exact'
    assert _platform_stock(empty_db, pid) == 93, (
        'the old 5 must be given back before the corrected 7 is taken')


def test_a_removed_line_gets_its_deduction_back(empty_db):
    """A sale line that vanishes from the source file is deleted, but the
    platform deduction it caused is not reversed.

    The sale did not happen, so the listing goes back to where the file left it.
    """
    import models
    pid = _seed(empty_db, 'B960', listing_stock=100)
    other = _seed(empty_db, 'B961', listing_stock=100)

    # One document, two lines: ours plus a second line that keeps the document
    # present in next week's file (removal detection is scoped to doc_base the
    # file mentions, so a doc that vanishes entirely is never reversed).
    models.import_weekly([_entry('IV910', 'B960', 5),
                          _entry('IV910', 'B961', 1)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95          # CONTROL
    assert _platform_stock(empty_db, other) == 99        # CONTROL

    stats = models.import_weekly([_entry('IV910', 'B961', 1)], 'sales', 'w2')

    # CONTROL — the line really was detected as removed.
    assert stats['removed'] == 1, stats
    assert _stock(empty_db, pid) == 0, 'the warehouse ledger DID reverse it'
    assert _platform_stock(empty_db, pid) == 100, (
        'a removed sale must return the units it took off the listing')


# ── Provenance itself (mig 172) ─────────────────────────────────────────────

def _provenance(path, pid):
    c = _conn(path)
    rows = c.execute(
        "SELECT d.units FROM platform_stock_deductions d"
        " JOIN platform_skus s ON s.id = d.platform_sku_id"
        " WHERE s.internal_product_id = ?", (pid,)).fetchall()
    c.close()
    return [r['units'] for r in rows]


def test_a_clamped_deduction_records_what_it_actually_took(empty_db):
    """The reason provenance is per-listing and stores the APPLIED amount.

    A listing holding 3 against a sale of 10 gives up 3 — `MAX(0, stock - 10)`
    floors at 0. Recording the intended 10 would invent 7 units the moment the
    row is reversed, so the listing would come back at 10 instead of 3.
    """
    import models
    pid = _seed(empty_db, 'B970', listing_stock=3)
    keeper = _seed(empty_db, 'B972', listing_stock=100)   # keeps the doc in the file

    models.import_weekly([_entry('IV920', 'B970', 10),
                          _entry('IV920', 'B972', 1)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 0, 'CONTROL: the listing is emptied'
    assert _platform_stock(empty_db, keeper) == 99, 'CONTROL: the other line landed'
    assert _provenance(empty_db, pid) == [3], 'must record 3 applied, not 10 intended'

    # Remove our line: the listing gets back exactly the 3 it gave up.
    stats = models.import_weekly([_entry('IV920', 'B972', 1)], 'sales', 'w2')
    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 3, 'not 10 — only what really moved'
    assert _provenance(empty_db, pid) == [], 'the record is consumed by the reversal'


def test_a_replay_does_not_record_the_deduction_twice(empty_db):
    """Pass 2 re-posts every row of an affected product. A row already in
    replayed_ids must neither deduct again (PR #424) nor write a second
    provenance row — otherwise a later reversal would hand back double."""
    import models
    pid = _seed(empty_db, 'B971', listing_stock=100)

    models.import_weekly([_entry('IV930', 'B971', 5)], 'sales', 'w1')
    assert _provenance(empty_db, pid) == [5]          # CONTROL

    # A second, unrelated sale of the same product forces a pass-2 replay.
    models.import_weekly([_entry('IV931', 'B971', 2)], 'sales', 'w2')

    assert _platform_stock(empty_db, pid) == 93
    assert sorted(_provenance(empty_db, pid)) == [2, 5], (
        'one row per sale, each holding only its own units')


def test_a_platform_refresh_supersedes_the_recorded_deduction(empty_db):
    """Codex, 2026-08-25: the failure this guard exists to stop.

    A marketplace file overwrites platform_skus.stock with the platform's OWN
    number. Any deduction recorded before it is already reflected in — or
    superseded by — that figure, so adding it back later would invent stock the
    platform does not have. That is the oversell direction, the one that costs
    a real order.

    100 → sale of 5 → 95 → file says 92 → remove the sale. Without the
    invalidation this lands on 97 while the platform holds 92.
    """
    import models
    pid = _seed(empty_db, 'B980', listing_stock=100)
    keeper = _seed(empty_db, 'B981', listing_stock=100)

    models.import_weekly([_entry('IV940', 'B980', 5),
                          _entry('IV940', 'B981', 1)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95          # CONTROL
    assert _provenance(empty_db, pid) == [5]             # CONTROL

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
        'accounted for that sale')


def test_a_partial_export_only_supersedes_the_listings_it_carried(empty_db):
    """A Seller Center export can be category-filtered, and
    import_platform_skus upserts ONLY the rows it was given. A listing the file
    did not mention keeps the stock it already had, so its recorded deduction
    is still live and must survive — wiping it would make a later correction or
    removal silently restore nothing.
    """
    import models
    mentioned = _seed(empty_db, 'B990', listing_stock=100)
    absent = _seed(empty_db, 'B991', listing_stock=100)

    models.import_weekly([_entry('IV950', 'B990', 5),
                          _entry('IV950', 'B991', 4)], 'sales', 'w1')
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


def test_a_double_deduction_aborts_the_import_instead_of_being_absorbed(empty_db):
    """The provenance write is a bare INSERT so a duplicate cannot pass quietly.

    Simulates replay protection failing: a source row that already holds
    provenance is forced back to synced_to_stock=0, so pass 2 no longer sees it
    in replayed_ids and tries to deduct it a second time. That must raise and
    roll the whole import back — stock and provenance both untouched — rather
    than absorb the second hit into the recorded total and leave the listing
    silently short until someone corrects the line.
    """
    import sqlite3
    import pytest
    import models
    pid = _seed(empty_db, 'B995', listing_stock=100)

    models.import_weekly([_entry('IV960', 'B995', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95        # CONTROL
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    # Break replay protection: the row keeps its provenance but looks unsynced.
    c = _conn(empty_db)
    c.execute("UPDATE sales_transactions SET synced_to_stock=0 WHERE doc_no='IV960'")
    c.commit()
    c.close()

    with pytest.raises(sqlite3.IntegrityError):
        models.import_weekly([_entry('IV961', 'B995', 1)], 'sales', 'w2')

    assert _platform_stock(empty_db, pid) == 95, 'the failed import must not land'
    assert _provenance(empty_db, pid) == [5], 'and must not add a second record'


def test_a_listing_with_no_stock_figure_records_nothing(empty_db):
    """`platform_skus.stock` is nullable and two live Lazada listings sit that
    way today (1481524654_TH-2052416252{3,4} — in Sendy, absent from every
    export since 2026-07-12). `MAX(0, NULL - n)` is NULL, so the deduction is a
    no-op there; the provenance write must agree and record nothing rather than
    claim units that never moved.
    """
    import models
    pid = _seed(empty_db, 'B996', listing_stock=100)
    c = _conn(empty_db)
    c.execute("UPDATE platform_skus SET stock=NULL WHERE internal_product_id=?", (pid,))
    c.commit()
    c.close()

    models.import_weekly([_entry('IV970', 'B996', 5)], 'sales', 'w1')

    assert _stock(empty_db, pid) == -5, 'CONTROL: the warehouse ledger still moves'
    assert _platform_stock(empty_db, pid) is None, 'the listing stays unknown'
    assert _provenance(empty_db, pid) == [], 'nothing moved, so nothing is recorded'


# ── Customer returns (SR) ───────────────────────────────────────────────────
#
# Put, 2026-08-25: "ในระบบของ Shopee และ Lazada หากลูกค้ายกเลิกคำสั่งซื้อหรือมีการ
# คืนสินค้า สต็อกสินค้าจะเพิ่ม (คืนเข้าคลัง) อัตโนมัติ เมื่อกระบวนการคืนเงิน/คืนสินค้า
# เสร็จสมบูรณ์." platform_skus.stock mirrors the platform's own number, so it has
# to move the same way. The old code skipped returns entirely, leaving the
# mirror LOW by the returned quantity until the next file import — the false-🔴
# direction, and 93 marketplace SR rows / 181 units have gone through it.

def test_a_customer_return_puts_the_units_back(empty_db):
    """Buy 5, return 5: the listing ends where it started, because that is what
    the platform itself did."""
    import models
    pid = _seed(empty_db, 'C100', listing_stock=100)

    models.import_weekly([_entry('IV800', 'C100', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95        # CONTROL
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    models.import_weekly([_entry('SR800', 'C100', 5)], 'sales', 'w2')

    assert _stock(empty_db, pid) == 0, 'the warehouse ledger takes the goods back in'
    assert _platform_stock(empty_db, pid) == 100, 'and the listing mirrors it'
    assert sorted(_provenance(empty_db, pid)) == [-5, 5], (
        'the return is recorded as a NEGATIVE removal, not as a second sale')


def test_a_deleted_return_line_takes_the_credit_back(empty_db):
    """Symmetry: if the return line itself is removed from the source, the
    units it handed back have to come off again. Recording credits unsigned
    would have left this one un-reversible — a fresh instance of the very bug
    this table exists to remove."""
    import models
    pid = _seed(empty_db, 'C101', listing_stock=100)
    keeper = _seed(empty_db, 'C102', listing_stock=100)

    models.import_weekly([_entry('IV810', 'C101', 5)], 'sales', 'w1')
    models.import_weekly([_entry('SR810', 'C101', 5),
                          _entry('SR810', 'C102', 1)], 'sales', 'w2')
    assert _platform_stock(empty_db, pid) == 100       # CONTROL
    assert _platform_stock(empty_db, keeper) == 101    # CONTROL

    stats = models.import_weekly([_entry('SR810', 'C102', 1)], 'sales', 'w3')

    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 95, (
        'the return never happened, so its credit comes back off')
    assert _provenance(empty_db, pid) == [5], 'only the original sale remains'


def test_a_return_on_a_listing_with_no_stock_figure_stays_unknown(empty_db):
    """NULL means "we have no figure", and a return must not invent one."""
    import models
    pid = _seed(empty_db, 'C103', listing_stock=100)
    c = _conn(empty_db)
    c.execute("UPDATE platform_skus SET stock=NULL WHERE internal_product_id=?", (pid,))
    c.commit()
    c.close()

    models.import_weekly([_entry('SR820', 'C103', 5)], 'sales', 'w1')

    assert _stock(empty_db, pid) == 5, 'CONTROL: the warehouse ledger still takes it in'
    assert _platform_stock(empty_db, pid) is None
    assert _provenance(empty_db, pid) == []


def _add_listing(path, pid, variation_id, stock):
    """A SECOND platform listing on the same product — the shape that makes the
    walk's listing choice observable."""
    c = _conn(path)
    c.execute(
        "INSERT INTO platform_skus (platform, product_name, variation_id,"
        " internal_product_id, qty_per_sale, stock)"
        " VALUES ('shopee', ?, ?, ?, 1, ?)", (variation_id, variation_id, pid, stock))
    c.commit()
    c.close()


def _platform_stocks(path, pid):
    c = _conn(path)
    rows = c.execute(
        "SELECT variation_id, stock FROM platform_skus WHERE internal_product_id=?"
        " ORDER BY variation_id", (pid,)).fetchall()
    c.close()
    return {r['variation_id']: r['stock'] for r in rows}


def test_undoing_a_return_cannot_drive_a_listing_negative(empty_db):
    """Codex, 2026-08-25. Undoing a RETURN moves stock DOWN, and the units it
    handed back may already have been sold again. Every other downward write in
    the module clamps at zero; this one is the new downward path.

    5 → SR credits 5 → 10 → a later sale takes 8 → 2 → delete the SR. The raw
    subtraction lands on −3; clamped it lands on 0.
    """
    import models
    pid = _seed(empty_db, 'C110', listing_stock=5)
    keeper = _seed(empty_db, 'C111', listing_stock=100)

    models.import_weekly([_entry('SR830', 'C110', 5),
                          _entry('SR830', 'C111', 1)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 10        # CONTROL: credited

    models.import_weekly([_entry('IV830', 'C110', 8)], 'sales', 'w2')
    assert _platform_stock(empty_db, pid) == 2         # CONTROL: sold down again

    stats = models.import_weekly([_entry('SR830', 'C111', 1)], 'sales', 'w3')

    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 0, 'clamped, not -3'


def test_a_two_listing_return_restores_the_total_not_the_split(empty_db):
    """The documented limit of the shared walk, pinned so it is not mistaken
    for a per-listing mirror.

    Both directions rank by CURRENT stock and the sale itself changes that
    ranking: A=10 B=9, a 5-unit sale takes A to 5, and the return then finds B
    on top. The product's platform TOTAL comes back to 19 — which is what the
    red/amber flags and the stock-sync targets read — while the split sits at
    A=5 B=14 where the platform holds A=10 B=9. The next authoritative file
    import corrects it.
    """
    import models
    pid = _seed(empty_db, 'C120', listing_stock=10)          # listing A
    _add_listing(empty_db, pid, 'VB-C120', stock=9)          # listing B

    models.import_weekly([_entry('IV840', 'C120', 5)], 'sales', 'w1')
    after_sale = _platform_stocks(empty_db, pid)
    assert after_sale == {'VC120': 5, 'VB-C120': 9}, after_sale   # CONTROL

    models.import_weekly([_entry('SR840', 'C120', 5)], 'sales', 'w2')

    after_return = _platform_stocks(empty_db, pid)
    assert sum(after_return.values()) == 19, 'the platform TOTAL is restored'
    assert after_return == {'VC120': 5, 'VB-C120': 14}, (
        'and the split is knowingly approximate — see the walk comment')


def test_a_hand_typed_stock_figure_supersedes_the_record(empty_db):
    """The other door into platform_skus.stock. `update_platform_sku` is the
    /ecommerce SKU edit form, and a hand-typed figure is authoritative for that
    listing exactly like a file is — so it has to supersede the record, or
    reversing it later invents units the operator's number already accounts
    for (Codex, 2026-08-25)."""
    import models
    pid = _seed(empty_db, 'C130', listing_stock=100)
    keeper = _seed(empty_db, 'C131', listing_stock=100)

    models.import_weekly([_entry('IV850', 'C130', 5),
                          _entry('IV850', 'C131', 1)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 95        # CONTROL
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    c = _conn(empty_db)
    sku_id = c.execute("SELECT id FROM platform_skus WHERE internal_product_id=?",
                       (pid,)).fetchone()['id']
    c.close()
    models.update_platform_sku(sku_id, price=10.0, special_price=None,
                               stock=92, qty_per_sale=1)

    assert _provenance(empty_db, pid) == [], 'the typed figure supersedes it'

    stats = models.import_weekly([_entry('IV850', 'C131', 1)], 'sales', 'w2')
    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 92, 'stays at what the operator typed, not 97'


def test_a_price_only_edit_keeps_the_record(empty_db):
    """The edit form resubmits every field, so a price-only save carries the
    unchanged stock with it. Discarding live provenance there would make a
    later reversal restore nothing — the opposite error."""
    import models
    pid = _seed(empty_db, 'C140', listing_stock=100)
    keeper = _seed(empty_db, 'C141', listing_stock=100)

    models.import_weekly([_entry('IV860', 'C140', 5),
                          _entry('IV860', 'C141', 1)], 'sales', 'w1')
    assert _provenance(empty_db, pid) == [5]           # CONTROL

    c = _conn(empty_db)
    sku_id = c.execute("SELECT id FROM platform_skus WHERE internal_product_id=?",
                       (pid,)).fetchone()['id']
    c.close()
    models.update_platform_sku(sku_id, price=12.5, special_price=None,
                               stock=95, qty_per_sale=1)   # stock unchanged

    assert _provenance(empty_db, pid) == [5], 'nothing moved, so the record stands'

    stats = models.import_weekly([_entry('IV860', 'C141', 1)], 'sales', 'w2')
    assert stats['removed'] == 1, stats
    assert _platform_stock(empty_db, pid) == 100, 'the sale is properly reversed'


def test_a_price_only_correction_never_touches_stock(empty_db):
    """Codex, 2026-08-25. Pass 1 replaces a source row on ANY change, price
    included — and reverse-then-reapply is LOSSY, because the undo clamps at
    zero. When the stock-affecting identity (product, platform customer,
    quantity, unit) is unchanged, the stock event did not change either, so the
    record is carried over to the replacement and the replacement is marked
    replayed. No reversal, no re-credit, no drift.

    Walked on the hostile state: file 5, SR credits 5 -> 10, a sale takes
    8 -> 2. Correcting the SR's PRICE must leave 2 exactly.
    """
    import models
    pid = _seed(empty_db, 'C150', listing_stock=5)

    models.import_weekly([_entry('SR850', 'C150', 5)], 'sales', 'w1')
    assert _platform_stock(empty_db, pid) == 10        # CONTROL
    models.import_weekly([_entry('IV870', 'C150', 8)], 'sales', 'w2')
    assert _platform_stock(empty_db, pid) == 2         # CONTROL

    stats = models.import_weekly([_entry('SR850', 'C150', 5, price=99.0)], 'sales', 'w3')

    assert stats['overwritten'] == 1, stats
    assert _platform_stock(empty_db, pid) == 2, 'a price fix is not a stock event'
    assert sorted(_provenance(empty_db, pid)) == [-5, 8], (
        'both records intact — the return carried to the replacement rather '
        'than being destroyed and remade, and the sale untouched')


def test_a_quantity_correction_after_the_credit_was_sold_stays_approximate(empty_db):
    """The residual, pinned. A correction that really DOES change the stock
    event has to reverse, and the undo clamps: file 5, SR credits 5 -> 10, a
    sale takes 8 -> 2, then the SR quantity is corrected 5 -> 4. Undoing wants
    2 - 5 = -3 and floors at 0; the replacement credits 4 onto 0 = 4. Replaying
    the true history gives 5 + 4 - 8 = 1.

    NOT mathematically unreconstructable — Codex is right that current stock
    plus signed provenance recovers the baseline, and the active rows could be
    replayed chronologically to rebuild it exactly. What blocks that today is
    ORDERING: transactions.created_at is the business date at second
    resolution, so there is no deterministic total order to replay. Fixing it
    means adding an event sequence, which is its own change. Until then the
    compound event needed is a return, then enough later sales to consume its
    credit, then a QUANTITY correction to that same return line; the error is
    bounded by the clamped amount and the next authoritative import replaces
    the figure outright.
    """
    import models
    pid = _seed(empty_db, 'C160', listing_stock=5)

    models.import_weekly([_entry('SR860', 'C160', 5)], 'sales', 'w1')
    models.import_weekly([_entry('IV880', 'C160', 8)], 'sales', 'w2')
    assert _platform_stock(empty_db, pid) == 2         # CONTROL

    stats = models.import_weekly([_entry('SR860', 'C160', 4)], 'sales', 'w3')

    assert stats['overwritten'] == 1, stats
    assert _platform_stock(empty_db, pid) == 4, (
        'pinned: the clamped undo cannot be replayed without an event order; '
        'the true history gives 1')


def test_a_clamped_reversal_is_reported_not_swallowed(empty_db):
    """Codex, 2026-08-25: the code already knows the undo was lossy and threw
    the signal away. It is not fatal — nothing decides on platform_skus.stock —
    but it is the only moment anything knows the figure went approximate, so
    import_weekly counts it and the route flashes it.

    Same consumed-credit state as the pinned quantity-correction case.
    """
    import models
    pid = _seed(empty_db, 'C170', listing_stock=5)

    models.import_weekly([_entry('SR870', 'C170', 5)], 'sales', 'w1')
    clean = models.import_weekly([_entry('IV890', 'C170', 8)], 'sales', 'w2')
    # CONTROL — an ordinary import reports zero, so the counter is not just
    # always-on noise.
    assert clean['lossy_platform_reversals'] == 0, clean

    lossy = models.import_weekly([_entry('SR870', 'C170', 4)], 'sales', 'w3')

    assert lossy['overwritten'] == 1, lossy
    assert lossy['lossy_platform_reversals'] == 1, lossy


def test_the_lossy_warning_is_one_helper_used_by_every_import_entry_point():
    """Cross-cutting sweep, left behind as a test per
    .claude/rules/erp-engineering-discipline.md. There are TWO routes that call
    import_weekly — the unified upload box and the Express DBF upload — and the
    first version of this warning was wired to only one of them (Codex,
    2026-08-25). Both now go through `_lossy_reversal_warning`, and the stats
    key is read in exactly one place so a third entry point cannot quietly
    skip the signal.
    """
    import os
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
