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
from .wacc import recalculate_product_wacc


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


def get_pending_mappings():
    """Return all BSN codes not yet mapped and not ignored."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM product_code_mapping
        WHERE product_id IS NULL AND is_ignored = 0
        ORDER BY bsn_code
    """).fetchall()
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
    """Ledger rows (transactions.note LIKE 'BSN%') that were posted for one of
    `bsn_code`'s docs but now sit on a product_id the CURRENT source row
    (sales_transactions/purchase_transactions) disagrees with — the exact
    "orphan" shape scripts/remap_bsn_code.py's old bug produced (source moved
    to the new product, ledger stranded on the old one). Independent of
    repoint_bsn_code's own bookkeeping — re-derived fresh from the ledger +
    source tables so it also catches orphans from OUTSIDE this function
    (e.g. left over from the old buggy script) for the same bsn_code.
    """
    sales_orphans = conn.execute(
        """
        SELECT t.id FROM transactions t
        WHERE t.note LIKE 'BSN ขาย%'
          AND t.reference_no IN (SELECT doc_no FROM sales_transactions WHERE bsn_code=?)
          AND NOT EXISTS (
              SELECT 1 FROM sales_transactions s
              WHERE s.doc_no = t.reference_no AND s.bsn_code = ? AND s.product_id = t.product_id
          )
        """,
        (bsn_code, bsn_code),
    ).fetchall()
    purchase_orphans = conn.execute(
        """
        SELECT t.id FROM transactions t
        WHERE t.note LIKE 'BSN ซื้อ%'
          AND t.reference_no IN (SELECT doc_no FROM purchase_transactions WHERE bsn_code=?)
          AND NOT EXISTS (
              SELECT 1 FROM purchase_transactions p
              WHERE p.doc_no = t.reference_no AND p.bsn_code = ? AND p.product_id = t.product_id
          )
        """,
        (bsn_code, bsn_code),
    ).fetchall()
    return len(sales_orphans) + len(purchase_orphans)


def repoint_bsn_code(conn, bsn_code: str, new_pid: int, bsn_unit=None) -> dict:
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

    Returns a report dict:
        {
          'affected_pids': sorted list[int],
          'rows_moved': {'sales': int, 'purchase': int},
          'orphan_rows_after': int (must be 0),
          'stock_before': {pid: qty}, 'stock_after': {pid: qty},
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
    try:
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
        _sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
        _sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')

        # ── 6. Recompute WACC for every affected product ────────────────────
        for pid in affected:
            recalculate_product_wacc(pid, conn)

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
        }
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()
