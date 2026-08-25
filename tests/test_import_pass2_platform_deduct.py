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


def _seed(path, code, listing_stock, unit_type='ตัว'):
    """A product mapped to `code`, plus one Shopee listing holding
    `listing_stock` units. Everything the test asserts is forced here rather
    than inherited: empty_db clones the live SCHEMA but the rows are ours."""
    c = _conn(path)
    _ensure_bsn_unit(c)
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


# ── The two paths that stay approximate, pinned so they are visible ──────────
#
# Codex adversarial review, 2026-08-25: a CHANGED line and a REMOVED line both
# leave platform_skus.stock off by a bounded amount, because nothing records
# how much deduction a given source row already carries. Both predate this fix
# and both are strictly LESS wrong after it (before: the whole product history
# re-deducted every import; after: at most one line's worth). They are pinned
# rather than left undiscovered — and pinned at the value the code actually
# produces, with the exact answer named, so whoever implements per-row
# deduction accounting knows what these numbers are supposed to become.
#
# Why the exact fix is not folded in here: reversing a deduction needs the
# amount that was ACTUALLY applied per listing, not the amount intended. The
# deduction walks listings ORDER BY stock DESC and clamps at MAX(0, ...), so a
# truncated deduction cannot be reversed from the source row alone — it needs a
# stored per-row, per-listing record, i.e. a migration on a stock-mutating path.

def test_a_corrected_line_over_deducts_by_the_old_quantity(empty_db):
    """A sale corrected 5 -> 7. Pass 1 DELETEs the old source row and INSERTs a
    replacement with a fresh id, so the replacement is not in replayed_ids and
    deducts its full 7 on top of the 5 already taken.

    Pinned at 88. Exact would be 93 (the +2 delta only). Keying replayed_ids on
    a stable business identity instead would give 95 — under-deducting by the
    delta — so it is not a fix either, just a different error.
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
    assert _platform_stock(empty_db, pid) == 88, (
        'known bounded gap: 95 - 7 = 88; exact would be 93')


def test_a_removed_line_never_gets_its_deduction_back(empty_db):
    """A sale line that vanishes from the source file is deleted, but the
    platform deduction it caused is not reversed.

    Pinned at 95. Exact would be 100 — the sale did not happen, so the listing
    should be back where the file left it. Pre-existing: the deduction has
    never been reversible on any path (see _sync_bsn_to_stock's docstring).
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
    assert _platform_stock(empty_db, pid) == 95, (
        'known gap: the deduction is not reversed; exact would be 100')
