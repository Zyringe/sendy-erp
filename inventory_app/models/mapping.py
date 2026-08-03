"""Product-code mapping (BSN <-> internal SKU) — extracted verbatim from
models.py (behavior-preserving split, Phase 12) — see models/__init__.py's
module docstring for the overall file-split rationale. No behavior changes.

Imports `_sync_bsn_to_stock` from `.bsn_sync` (expected edge, per the brief)
and `recalculate_product_wacc` from `.wacc` (not on the brief's original edge
list, but forced by the verbatim body: `repoint_bsn_code` calls it directly
for every affected product) — acyclic, flagged in the Phase 12 report.
"""
from database import get_connection
import bsn_units

from .bsn_sync import (_BSN_LEDGER_NOTE_PATTERNS, _sync_bsn_to_stock,
                       cross_unit_hazard)
from .wacc import (recalculate_product_wacc, preflight_batch,
                   WaccIdentityError)
from .system_alerts import record_wacc_identity_alert


def upsert_mapping(bsn_code: str, bsn_name: str, product_id=None, is_ignored=0,
                   ignore_reason=None, bsn_unit=''):
    """Upsert a mapping row by (bsn_code, bsn_unit) (mig 124 restore).

    Default bsn_unit='' matches/creates only the non-split catch-all row, so
    a generic caller that doesn't know about units (the /mapping UI today)
    never clobbers a unit-specific split row created elsewhere.

    Uses UPDATE-then-INSERT to avoid dependency on which UNIQUE constraint is
    currently active (compatible across the mig-112/mig-124 boundary).
    """
    bsn_unit = bsn_unit or ''
    conn = get_connection()
    updated = conn.execute("""
        UPDATE product_code_mapping SET
            bsn_name      = ?,
            product_id    = ?,
            is_ignored    = ?,
            ignore_reason = ?
        WHERE bsn_code = ? AND bsn_unit = ?
    """, (bsn_name, product_id, is_ignored, ignore_reason, bsn_code, bsn_unit)).rowcount
    if not updated:
        conn.execute("""
            INSERT OR IGNORE INTO product_code_mapping
                (bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit))
    conn.commit()
    conn.close()


def get_pending_mappings(conn=None):
    """Return all BSN codes not yet mapped and not ignored."""
    owned = conn is None
    if owned:
        conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM product_code_mapping
        WHERE product_id IS NULL AND is_ignored = 0
        ORDER BY bsn_code
    """).fetchall()
    if owned:
        conn.close()
    return rows


def get_pending_split_mappings():
    """BSN codes whose UNSYNCED ledger rows sit on a product whose unit_type
    disagrees with the row's (normalized) unit in a way cross_unit_hazard()
    forbids a ratio for (pair or pack_piece) — the wrong-unit-catch-all-mapping
    case /unit-conversions can't fix with a ratio. Grouped by
    (bsn_code, normalized unit, product_id) across sales+purchase so the
    /mapping split-section can offer a per-unit split mapping instead."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT bsn_code, unit, product_id,
               COUNT(*) AS row_count,
               MIN(doc_no) AS example_doc,
               MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
          FROM sales_transactions
         WHERE product_id IS NOT NULL AND bsn_code IS NOT NULL AND synced_to_stock = 0
         GROUP BY bsn_code, unit, product_id
        UNION ALL
        SELECT bsn_code, unit, product_id,
               COUNT(*) AS row_count,
               MIN(doc_no) AS example_doc,
               MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
          FROM purchase_transactions
         WHERE product_id IS NOT NULL AND bsn_code IS NOT NULL AND synced_to_stock = 0
         GROUP BY bsn_code, unit, product_id
    """).fetchall()

    groups = {}
    for r in rows:
        norm_unit = bsn_units.normalize_unit(r['unit']) or ''
        key = (r['bsn_code'], norm_unit, r['product_id'])
        g = groups.setdefault(key, {'row_count': 0, 'example_doc': None, 'bsn_raw_name': None})
        g['row_count'] += r['row_count']
        if g['example_doc'] is None or (r['example_doc'] and r['example_doc'] < g['example_doc']):
            g['example_doc'] = r['example_doc']
        if g['bsn_raw_name'] is None and r['bsn_raw_name']:
            g['bsn_raw_name'] = r['bsn_raw_name']

    out = []
    for (bsn_code, norm_unit, product_id), g in groups.items():
        hazard = cross_unit_hazard(conn, product_id, norm_unit)
        if hazard is None:
            continue
        product = conn.execute(
            "SELECT product_name, unit_type FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        out.append({
            'bsn_code': bsn_code,
            'bsn_unit': norm_unit,
            'product_id': product_id,
            'product_name': product['product_name'] if product else None,
            'unit_type': product['unit_type'] if product else None,
            'row_count': g['row_count'],
            'example_doc': g['example_doc'],
            'bsn_raw_name': g['bsn_raw_name'],
            'hazard': hazard,
        })
    conn.close()
    out.sort(key=lambda d: d['bsn_code'])
    return out


def resolve_pending_mappings(conn):
    """
    เติม product_id ให้แถว BSN ที่ยังไม่มี แล้ว sync ไปยัง stock ทันที

    Unit-aware (mig 124 restore): resolves each pending row through
    _resolve_mapping(conn, bsn_code, unit) instead of a bare bsn_code join —
    once a code is split (e.g. 030บ3412 → 81 for แผง, 111 for ตัว), a plain
    "first row for this code" join would backfill a pending row to an
    arbitrary unit's product. Row-by-row in Python because
    _resolve_mapping's unit normalization + blank-catch-all tiebreak can't be
    expressed as a single correlated-subquery UPDATE.

    Historical PURCHASE rows store the RAW BSN unit acronym (e.g. 'ตว');
    SALES rows are normalized at import time. _resolve_mapping normalizes
    its `unit` arg itself, so passing the stored `unit` straight in resolves
    correctly for both tables regardless of which form is on disk.

    For any non-split code (the current live state — only a blank bsn_unit=''
    catch-all row exists) this returns exactly what the old bare bsn_code
    join did: one row, any unit resolves to it.
    """
    for table, file_type in (
        ('sales_transactions',    'sales'),
        ('purchase_transactions', 'purchase'),
    ):
        pending = conn.execute(
            f"SELECT id, bsn_code, unit FROM {table} "
            f"WHERE product_id IS NULL AND bsn_code IS NOT NULL"
        ).fetchall()
        for row in pending:
            product_id, is_ignored, mapped = _resolve_mapping(
                conn, row['bsn_code'], row['unit']
            )
            if mapped and not is_ignored and product_id is not None:
                conn.execute(
                    f"UPDATE {table} SET product_id = ? WHERE id = ?",
                    (product_id, row['id'])
                )
        # sync แถวที่เพิ่ง resolve ไปยัง transactions/stock
        _sync_bsn_to_stock(conn, table, file_type)
    conn.commit()


def _resolve_mapping(conn, code, unit=''):
    """(product_id, is_ignored, mapped?) — unit-aware bsn_code lookup (mig 124
    restore). A split bsn_code can map to DIFFERENT products per BSN unit
    (e.g. แผง vs ตัว); a blank bsn_unit row is the non-split catch-all and
    matches any unit that has no dedicated row.

    `unit` is normalized the same way import does (bsn_units.normalize_unit)
    so a raw acronym ('ตว') matches a mapping row stored in full-Thai ('ตัว').
    Omitting `unit` (default '') matches ONLY the blank/catch-all row —
    preserves the pre-restore pure-bsn_code behavior for callers that don't
    pass a unit.
    """
    unit = bsn_units.normalize_unit(unit) or ''
    m = conn.execute(
        "SELECT product_id, is_ignored FROM product_code_mapping "
        "WHERE bsn_code = ? AND bsn_unit IN (?, '') "
        "ORDER BY (bsn_unit = '') LIMIT 1",
        (code, unit)
    ).fetchone()
    return (m['product_id'] if m else None, m['is_ignored'] if m else 0, bool(m))


# _BSN_LEDGER_NOTE_PATTERNS is imported from models/bsn_sync.py (the module that
# WRITES those notes) so this rebuild and update_unit_conversion_ratio's cannot
# drift apart — they did: the ratio rebuild used to delete only the 'BSN%' half.


def _bsn_code_ledger_orphans(conn, bsn_code):
    """Ledger rows (transactions.note LIKE 'BSN%') on one of `bsn_code`'s docs
    that NO source row on that doc can account for — the exact "orphan" shape
    scripts/remap_bsn_code.py's old bug produced (source moved to the new
    product, ledger stranded on the old one). Independent of
    repoint_bsn_code's own bookkeeping — re-derived fresh from the ledger +
    source tables so it also catches orphans from OUTSIDE this function
    (e.g. left over from the old buggy script) for the same bsn_code.

    The NOT EXISTS deliberately does NOT filter on bsn_code. A BSN document is
    a multi-line bill: one doc_no carries many codes, each posting its own
    ledger row on its own product. Requiring the source row to ALSO match
    `bsn_code` flagged every sibling line on a shared bill as an orphan, so
    the count could never reach 0 for a code that shares bills with anything —
    measured 2026-08-03 on an UNTOUCHED database: code 999ล2030 reported 14
    "orphans", all of them other products' lines (กิ๊ปรัด ORBIT, สายยูตราจิงโจ้,
    ตะไบแทงเลื่อย, ...). That made repoint_bsn_code's documented
    "orphan_rows_after must be 0" invariant unsatisfiable, which is worse than
    a missing check: it trains the reader to ignore a real alarm.

    "Explained by SOME source row on this doc at this product" is the property
    that actually distinguishes a stranded row from a sibling line.

    Purchase rows do better than that. mig 148 records which line each IN came
    from (`source_bsn_code` / `source_line_seq`, 4,005 of 4,049 rows), so for
    those the exact line is checked instead — no sibling can explain a stranded
    row away, and a sibling's own stranding is caught too. Rows without
    provenance fall back to the doc+product test: every sales row (by design —
    sales_transactions has no line_seq, see bsn_sync) and the 44 legacy
    GR/'BSN ซื้อ-คืน' purchase rows. Those keep the narrow blind spot: a
    stranded row is masked if its product happens to carry another line of the
    same document. That trade is deliberate — the previous form reported a
    false alarm on every shared bill, which is a check nobody can act on.
    """
    sales_orphans = conn.execute(
        """
        SELECT t.id FROM transactions t
        WHERE t.note LIKE 'BSN ขาย%'
          AND t.reference_no IN (SELECT doc_no FROM sales_transactions WHERE bsn_code=?)
          AND NOT EXISTS (
              SELECT 1 FROM sales_transactions s
              WHERE s.doc_no = t.reference_no AND s.product_id = t.product_id
          )
        """,
        (bsn_code,),
    ).fetchall()
    purchase_orphans = conn.execute(
        """
        SELECT t.id FROM transactions t
        WHERE t.note LIKE 'BSN ซื้อ%'
          AND t.reference_no IN (SELECT doc_no FROM purchase_transactions WHERE bsn_code=?)
          AND CASE WHEN t.source_bsn_code IS NOT NULL THEN
                   NOT EXISTS (
                       SELECT 1 FROM purchase_transactions p
                       WHERE p.doc_no = t.reference_no
                         AND p.bsn_code = t.source_bsn_code
                         AND p.line_seq = t.source_line_seq
                         AND p.product_id = t.product_id
                   )
              ELSE
                   NOT EXISTS (
                       SELECT 1 FROM purchase_transactions p
                       WHERE p.doc_no = t.reference_no AND p.product_id = t.product_id
                   )
              END
        """,
        (bsn_code,),
    ).fetchall()
    return len(sales_orphans) + len(purchase_orphans)


def missing_unit_ratios(conn, new_pid: int, rows) -> list:
    """BSN units among `rows` that product `new_pid` could NOT convert, sorted.

    _sync_bsn_to_stock SKIPS a row whose (product, bsn_unit) has no ratio —
    silently, and without marking it — so a destination missing the conversion
    swallows that side of the history while the other side posts. Measured
    2026-08-03 re-pointing 999ล2030 onto pid 791, which had no unit_conversions
    row at all: all 9 purchase rows vanished, the 13 sales rows landed, and
    stock went 24 -> -558.

    Mirrors bsn_sync._get_base_qty's resolution exactly — unit_type match
    first, then the unit_conversions lookup — so this check cannot disagree
    with the sync it guards. Shared by repoint_bsn_code (which refuses) and
    scripts/remap_bsn_code.py's dry-run (which warns), so a preview can never
    say "fine" about a move that the apply will reject.
    """
    product = conn.execute(
        "SELECT unit_type FROM products WHERE id=?", (new_pid,)
    ).fetchone()
    if product is None:
        # repoint_bsn_code validates this before calling, but the CLI dry-run
        # calls us directly — without this it dies on a bare TypeError.
        raise ValueError(f"missing_unit_ratios: product {new_pid} not found")
    unit_type = product['unit_type']
    missing = set()
    for r in rows:
        unit = r['unit']
        if unit is not None and unit.strip() == (unit_type or '').strip():
            continue
        if conn.execute(
            "SELECT 1 FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
            (new_pid, unit),
        ).fetchone():
            continue
        missing.add(unit)
    return sorted(missing)


def repoint_bsn_code(conn, bsn_code: str, new_pid: int, bsn_unit=None,
                     preserve_stock: bool = False) -> dict:
    """Canonical single bsn_code re-point — moves the mapping AND the code's
    FULL historical ledger onto `new_pid`, not just the source tables.

    Root-cause fix for scripts/remap_bsn_code.py's old bug: that script only
    UPDATEd sales_transactions/purchase_transactions.product_id and recalced
    stock_levels via a raw SUM(quantity_change); it never touched the
    `transactions` ledger rows tagged 'BSN%', so those stayed STRANDED on the
    OLD product (source says new_pid, ledger still says old pid — an orphan)
    and new_pid's stock never actually reflected the moved sales/purchases.
    398 such orphans across 141 products were produced this way (see
    decisions/log.md 2026-07-02/07-03).

    Implements the proven Reconciliation Procedure (scripts/p2p3_split_hinges.py
    + sendy_erp/CLAUDE.md "ลบ BSN sync" / "merge product" notes): re-point the
    source rows → reset synced_to_stock=0 → DELETE the stale ledger for every
    AFFECTED product → re-sync via the real `_sync_bsn_to_stock` → recompute
    WACC. The mig-080 `after_transaction_delete`/`_insert` triggers keep
    stock_levels correct automatically through the delete+resync — no manual
    stock_levels surgery here.

    bsn_unit: None (default) re-points the WHOLE code — every
    product_code_mapping row for `bsn_code` (regardless of bsn_unit) and every
    sales_transactions/purchase_transactions row for it — the common case,
    equivalent to the old CLI's bare --code/--to. Pass a specific bsn_unit
    (already-normalized full-Thai, e.g. 'แผง') to re-point only THAT
    unit-scoped slice of a SPLIT code (mig 124 unit-aware mapping): the
    mapping/source-row scan is filtered to that unit on both sides, so a
    sibling unit's row/product (e.g. a code split แผง→A, ตัว→B) is never
    touched when only แผง is being redirected elsewhere. `bsn_unit` is
    normalized via bsn_units.normalize_unit so a raw acronym also works.

    Deliberate deviation from a literal "reuse upsert_mapping()" — that helper
    opens its OWN get_connection() and commits/closes on its own, which
    (exactly like p2p3_split_hinges's own docstring explains for the same
    reason) risks a `database is locked` timeout or a partial commit that
    breaks this function's single-transaction/rollback guarantee if called
    mid-transaction on the same sqlite file. Reproduces upsert_mapping's
    UPDATE-then-INSERT logic directly on `conn` instead (p2p3's proven
    pattern).

    preserve_stock: hold every affected product's stock level exactly where it
    was. Re-pointing moves a code's whole IN/OUT history to another product,
    but each product's OPENING stock was seeded while the code was still
    mis-attributed — so the origin product's opening balance silently covers
    goods that physically belong to the destination. Moving the history alone
    therefore drives one side negative (measured 2026-08-03: 532ข6740's 4-inch
    history moving 594 -> 1795 put 1795 at -12; 999ล2030 put 790 at -14).
    Nobody has re-counted the shelf, so inventing new stock numbers would be a
    guess: this posts one ADJUST per product that re-allocates exactly the
    quantity that moved, leaving every level untouched and the total across the
    affected set conserved. The ADJUST is backdated to the head of the affected
    set's ledger — it is an OPENING-balance re-allocation, the goods were on
    the shelf all along under the wrong product — so the WACC replay this
    function then runs never sees a phantom deficit. ADJUST is otherwise
    WACC-neutral (models/wacc.py treats it as a stock-only event).
    Raises RuntimeError if any level still moved once the ADJUSTs are posted.

    Returns a report dict:
        {
          'affected_pids': sorted list[int],
          'rows_moved': {'sales': int, 'purchase': int},
          'orphan_rows_after': int (must be 0),
          'stock_before': {pid: qty}, 'stock_after': {pid: qty},
          'stock_adjustments': {pid: qty} (only when preserve_stock),
        }

    Idempotent: re-running with the same args is a no-op (mapping already
    points to new_pid, source rows already there, ledger already rebuilt on
    new_pid — the delete-then-resync just rebuilds the same rows again).

    Caller owns the transaction: if `conn` is given, this does NOT commit or
    close it (caller commits); if `conn` is None, opens+commits+closes its own
    (matches the upsert_pack_unpack_pair / find_pair_partner conn convention).
    """
    own = conn is None
    if own:
        conn = get_connection()
    _conn_closed = False
    try:
        if own:
            # The whole operation is a check-then-write on stock: stock_before
            # is read up here and the compensating ADJUST that restores it is
            # written much further down. Railway runs gunicorn -w 2, so a
            # second worker moving stock in between is real — and with
            # preserve_stock that is actively harmful, because the ADJUST would
            # "restore" a level that a legitimate concurrent sale had already
            # changed, silently reverting it. Take the write lock before the
            # first read that feeds the decision (models/transactions.py
            # set_stock_to is the same pattern). Callers that supply `conn` own
            # the transaction and must have taken the lock themselves —
            # scripts/remap_bsn_code.py does.
            conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (new_pid,)).fetchone():
            raise ValueError(f"repoint_bsn_code: product {new_pid} not found")

        norm_unit = bsn_units.normalize_unit(bsn_unit) if bsn_unit else None

        # ── 1. Compute the affected product set (BEFORE any mutation) ──────
        if norm_unit is not None:
            mapping_rows = conn.execute(
                "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit=?",
                (bsn_code, norm_unit),
            ).fetchall()
        else:
            mapping_rows = conn.execute(
                "SELECT product_id FROM product_code_mapping WHERE bsn_code=?",
                (bsn_code,),
            ).fetchall()

        def _unit_scoped_source_rows(table):
            rows = conn.execute(
                f"SELECT id, unit, product_id FROM {table} WHERE bsn_code=?", (bsn_code,)
            ).fetchall()
            if norm_unit is None:
                return rows
            return [r for r in rows
                    if (bsn_units.normalize_unit(r['unit']) or '') == norm_unit]

        sales_rows = _unit_scoped_source_rows('sales_transactions')
        purchase_rows = _unit_scoped_source_rows('purchase_transactions')

        # ── 1a. The destination must be able to CONVERT every unit it is about
        # to receive — refuse up front, while nothing has been written ───────
        missing_units = missing_unit_ratios(
            conn, new_pid, sales_rows + purchase_rows)
        if missing_units:
            new_unit_type = conn.execute(
                "SELECT unit_type FROM products WHERE id=?", (new_pid,)
            ).fetchone()['unit_type']
            raise ValueError(
                f"repoint_bsn_code: product {new_pid} has no unit_conversions "
                f"ratio for {missing_units!r} (its unit_type is "
                f"{new_unit_type!r}). Those rows would be silently skipped by "
                f"the re-sync and their stock lost. Define the ratio first."
            )

        affected = {new_pid}
        for r in mapping_rows:
            if r['product_id'] is not None:
                affected.add(r['product_id'])
        for r in (sales_rows + purchase_rows):
            if r['product_id'] is not None:
                affected.add(r['product_id'])

        def _stock(pid):
            row = conn.execute(
                "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
            ).fetchone()
            return row['quantity'] if row else 0

        stock_before = {pid: _stock(pid) for pid in affected}

        # ── 2. Re-point product_code_mapping (reproduces upsert_mapping) ───
        existing_name_row = conn.execute(
            "SELECT bsn_name, is_ignored, ignore_reason FROM product_code_mapping "
            "WHERE bsn_code=? AND bsn_unit=?",
            (bsn_code, norm_unit if norm_unit is not None else ''),
        ).fetchone()
        if existing_name_row is None:
            any_row = conn.execute(
                "SELECT bsn_name, is_ignored, ignore_reason FROM product_code_mapping "
                "WHERE bsn_code=? LIMIT 1", (bsn_code,)
            ).fetchone()
            bsn_name = any_row['bsn_name'] if any_row else bsn_code
            is_ignored = any_row['is_ignored'] if any_row else 0
            ignore_reason = any_row['ignore_reason'] if any_row else None
        else:
            bsn_name = existing_name_row['bsn_name']
            is_ignored = existing_name_row['is_ignored']
            ignore_reason = existing_name_row['ignore_reason']

        if norm_unit is not None:
            target_unit_rows = [(norm_unit,)]
        else:
            target_unit_rows = [(r[0],) for r in conn.execute(
                "SELECT bsn_unit FROM product_code_mapping WHERE bsn_code=?", (bsn_code,)
            ).fetchall()] or [('',)]

        for (unit_value,) in target_unit_rows:
            updated = conn.execute(
                """
                UPDATE product_code_mapping SET
                    bsn_name=?, product_id=?, is_ignored=0, ignore_reason=NULL
                WHERE bsn_code=? AND bsn_unit=?
                """,
                (bsn_name, new_pid, bsn_code, unit_value),
            ).rowcount
            if not updated:
                conn.execute(
                    """
                    INSERT INTO product_code_mapping
                        (bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit)
                    VALUES (?, ?, ?, 0, NULL, ?)
                    """,
                    (bsn_code, bsn_name, new_pid, unit_value),
                )

        # ── 3. Re-point the code's source rows (unit-scoped) ────────────────
        def _repoint_rows(table, rows):
            for r in rows:
                conn.execute(
                    f"UPDATE {table} SET product_id=?, synced_to_stock=0 WHERE id=?",
                    (new_pid, r['id']),
                )
            return len(rows)

        rows_moved = {
            'sales': _repoint_rows('sales_transactions', sales_rows),
            'purchase': _repoint_rows('purchase_transactions', purchase_rows),
        }

        # ── 4. DELETE the stale BSN ledger for every affected product ───────
        # (mig-080 after_transaction_delete auto-reconciles stock_levels).
        ph = ",".join("?" * len(affected))
        note_ph = " OR ".join(["note LIKE ?"] * len(_BSN_LEDGER_NOTE_PATTERNS))
        conn.execute(
            f"DELETE FROM transactions WHERE product_id IN ({ph}) AND ({note_ph})",
            tuple(affected) + _BSN_LEDGER_NOTE_PATTERNS,
        )

        # Reset synced_to_stock=0 for ALL source rows now sitting on an
        # affected product (not just this bsn_code's) — the delete above wiped
        # ALL 'BSN%' ledger on those products regardless of code, so every
        # source row on them (any code, robust to a shared purchase doc_no
        # spanning several lines/products) must be rebuilt fresh too.
        for table in ('sales_transactions', 'purchase_transactions'):
            conn.execute(
                f"UPDATE {table} SET synced_to_stock=0 WHERE product_id IN ({ph})",
                tuple(affected),
            )

        # ── 5. Re-sync via the real app function ────────────────────────────
        # deduct_platform=False: this is a REPLAY of rows that already deducted
        # platform_skus.stock when they were first imported, and deleting their
        # ledger rows in step 4 did not put that stock back.
        # product_ids: and it must stay INSIDE the affected set — an unrelated
        # product's pending row swept in here would first-sync under that same
        # deduct_platform=False and lose the deduction it is owed.
        for _t, _ft in (('sales_transactions', 'sales'),
                        ('purchase_transactions', 'purchase')):
            _sync_bsn_to_stock(conn, _t, _ft, deduct_platform=False,
                               product_ids=affected)

        # ── 5b. Optionally hold every stock level where it was ──────────────
        # Runs BEFORE the WACC recompute so the ledger step 6 reads is final.
        # See the preserve_stock paragraph in this function's docstring for why
        # a re-point moves stock at all, and why re-allocating it beats
        # inventing counts nobody has taken.
        stock_adjustments = {}
        compensations_moved_late = []
        compensation_rowid = {}
        if preserve_stock:
            # Backdated to the head of the affected set's ledger, because this
            # IS an opening-balance re-allocation: the quantity was on the
            # shelf the whole time, just filed under the wrong product. Dating
            # it "today" instead leaves the replay short for the entire span of
            # history, which sends models/wacc.py down its `current_stock < 0`
            # branch and FREEZES the weighted average at a stale value — an
            # earlier revision of this code did exactly that and left pid 1795
            # at a frozen 18.2604 with stock_after -12 in its cost ledger.
            # STRICTLY earlier than the head, not equal to it. WACC sorts
            # `ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END,
            # id` (models/wacc.py) — so an IN sharing the head timestamp would
            # be costed BEFORE this ADJUST and see the deficit anyway, which is
            # the whole failure the backdating exists to avoid. 21 products in
            # the live catalogue have an IN at their earliest timestamp, so the
            # tie is reachable; a second's clearance removes the ordering
            # question entirely.
            #
            # The head is not always ABSORBABLE, though. When the compensation
            # is negative and larger than anything the product holds early on —
            # the shape a brand-new destination lands in — backdating starts the
            # replay under water instead, every purchase hits that same
            # `current_stock < 0` freeze, and cost_price is left at 0.00 despite
            # real priced purchases. Measured on prod 2026-08-03: pid 2054 came
            # out at 0.00 from ฿453 of purchases and had to be repaired by hand.
            # So: place at the head, then MEASURE, and fall back to today for
            # any product the head placement broke. Predicting it instead does
            # not work — pid 594's running balance also dips to -24 and its head
            # placement is perfectly fine, because the dip lands between
            # purchases and only a purchase costed while negative freezes WACC.
            opening_at = conn.execute(
                f"SELECT datetime(MIN(created_at), '-1 second') m"
                f" FROM transactions WHERE product_id IN ({ph})",
                tuple(affected),
            ).fetchone()['m']

            def _post_compensation(pid, delta, created_at):
                note = (f'ปรับยอดยกมา: ย้ายรหัส {bsn_code} '
                        f'(ยอดคงเหลือคงเดิมที่ {stock_before[pid]:g})')
                if created_at:
                    cur = conn.execute(
                        "INSERT INTO transactions (product_id, txn_type,"
                        " quantity_change, unit_mode, reference_no, note, created_at)"
                        " VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)",
                        (pid, delta, note, created_at),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO transactions (product_id, txn_type,"
                        " quantity_change, unit_mode, reference_no, note)"
                        " VALUES (?, 'ADJUST', ?, 'unit', NULL, ?)",
                        (pid, delta, note),
                    )
                return cur.lastrowid

            for pid in sorted(affected):
                delta = round(stock_before[pid] - _stock(pid), 4)
                if not delta:
                    continue
                compensation_rowid[pid] = _post_compensation(pid, delta, opening_at)
                stock_adjustments[pid] = delta

            # Compare at 4 dp, the precision the stock triggers themselves round
            # to (mig 092). stock_levels.quantity is REAL, so a bare != can fire
            # on IEEE-754 noise alone and abort a correct operation.
            drifted = {pid: (stock_before[pid], _stock(pid)) for pid in affected
                       if round(_stock(pid), 4) != round(stock_before[pid], 4)}
            if drifted:
                raise RuntimeError(
                    f"repoint_bsn_code({bsn_code!r}, preserve_stock=True): stock "
                    f"still moved after compensation — {drifted}"
                )

        # ── 6. Recompute WACC for every affected product ────────────────────
        # Pre-flight the whole set first, against the ledger this transaction
        # has just rebuilt (SQLite lets these reads see our own uncommitted
        # changes). Validating here rather than per-product stops a later
        # product's failure from leaving an earlier one rebuilt. Unlike the
        # import and conversion paths, a raise here unwinds the WHOLE remap:
        # the web caller passes conn=None so this function owns and rolls back
        # the transaction, and scripts/remap_bsn_code.py:115 already catches,
        # rolls back and closes.
        preflight_batch(conn, sorted(affected), operation='mapping_repoint')
        for pid in sorted(affected):
            recalculate_product_wacc(pid, conn)

        # ── 6b. Did the backdated compensation break the replay? ─────────────
        # See 5b: the head is the right place semantically, but a negative
        # compensation the product cannot absorb there puts every purchase
        # under water. `stock_after - qty_change` is the stock the WACC engine
        # saw BEFORE costing that lot, so a negative value is exactly the
        # `current_stock < 0` freeze having fired. Move that product's
        # compensation to today and recompute just that product.
        for pid, delta in sorted(stock_adjustments.items()):
            if delta > 0:
                # A positive compensation at the head can only RAISE the running
                # balance, so it can never be what put a purchase under water —
                # any such purchase was already there. Relocating it late would
                # instead REMOVE the help it is giving and re-create the very
                # deficit the backdating exists to close. Measured while
                # building this: letting the fallback touch positive
                # compensations moved pid 1795's cost from 17.5898 to 16.0311.
                continue
            broke = conn.execute(
                "SELECT 1 FROM product_cost_ledger WHERE product_id=?"
                " AND event_type='PURCHASE'"
                " AND ROUND(stock_after - qty_change, 4) < 0 LIMIT 1",
                (pid,),
            ).fetchone()
            if not broke:
                continue
            # Note this errs toward relocating: a product that ALREADY had a
            # negative-stock purchase before the re-point also matches here.
            # Moving a negative compensation late is safe either way — it can
            # only raise the running balance during history, never lower it —
            # so a false positive costs semantics, not correctness.
            # Move the row we just wrote, by rowid. Matching it back by note
            # would mean LIKE-ing a bsn_code straight into the pattern, where a
            # literal _ or % in a code silently widens the match onto another
            # code's compensation. mig-080's after_transaction_update reconciles
            # stock_levels; a timestamp-only change nets to zero there.
            conn.execute(
                "UPDATE transactions SET created_at = datetime('now','localtime')"
                " WHERE id = ?", (compensation_rowid[pid],))
            recalculate_product_wacc(pid, conn)
            compensations_moved_late.append(pid)

        if compensations_moved_late:
            # The stock guard above ran against the head placement; re-assert
            # it now that some rows carry a different timestamp.
            drifted = {pid: (stock_before[pid], _stock(pid)) for pid in affected
                       if round(_stock(pid), 4) != round(stock_before[pid], 4)}
            if drifted:
                raise RuntimeError(
                    f"repoint_bsn_code({bsn_code!r}): stock moved while relocating "
                    f"a compensation — {drifted}"
                )

        stock_after = {pid: _stock(pid) for pid in affected}
        orphan_rows_after = _bsn_code_ledger_orphans(conn, bsn_code)

        if own:
            conn.commit()

        return {
            'affected_pids': sorted(affected),
            'rows_moved': rows_moved,
            'orphan_rows_after': orphan_rows_after,
            'stock_before': stock_before,
            'stock_after': stock_after,
            'stock_adjustments': stock_adjustments,
            'compensations_moved_late': compensations_moved_late,
        }
    except WaccIdentityError as e:
        # The whole remap unwinds (unlike the import/conversion paths, nothing
        # of this operation stays committed). Record the alert only once the
        # failed transaction is rolled back AND closed, on a fresh connection —
        # writing it on `conn` would roll back with the failure, and opening a
        # second connection while this one still holds the write lock risks
        # "database is locked". When we do NOT own the connection we cannot
        # roll back or close it, so the alert is the supplying caller's
        # responsibility — scripts/remap_bsn_code.py does exactly that in its
        # own WaccIdentityError handler.
        if own:
            conn.rollback()
            conn.close()
            _conn_closed = True
            record_wacc_identity_alert(e, operation='mapping_repoint',
                                       extra={'bsn_code': bsn_code,
                                              'new_product_id': new_pid})
        raise
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if own and not _conn_closed:
            conn.close()
