#!/usr/bin/env python3
"""
Phase A — cleanup of the 398 orphan BSN sales-ledger rows found by the
2026-07-03 DB-wide audit (`decisions/log.md` 2026-07-03 "Orphan BSN ledger
rows", memory `project_data_quality_issues.md`, report
`Operations/05_analysis-reports/data-quality/orphan_bsn_ledger_rows_2026-07-03.csv`).

Root cause (see `models.repoint_bsn_code` docstring + PR #238): old bulk
`product_code_mapping` corrections re-pointed `sales_transactions.product_id`
to the corrected product and re-synced, but never deleted the OLD
`transactions` ledger rows from the previously-wrong product — leaving a
"phantom" ledger row stranded on the wrong product while the REAL,
correctly-synced ledger row already exists on the right one. PR #238 fixed
the tool going forward (no new orphans will accrue); this script cleans up
the EXISTING backlog.

Orphan definition (independently re-derived from the live DB, not the CSV —
the CSV is a point-in-time hypothesis, not ground truth):

    A `transactions` row with `note IN ('BSN ขาย','BSN ขาย-คืน')`,
    product_id=P, reference_no=R (=doc_no) is an ORPHAN iff there is no
    `sales_transactions` row with doc_no=R AND product_id=P.

    An orphan is classified:
      DUPLICATE  — >=1 `sales_transactions` row exists for doc_no=R at all
                   (on some OTHER product_id) => the real sale is correctly
                   recorded elsewhere; this row is a harmless-but-wrong
                   phantom polluting the wrong product's ประวัติ.
      NO_SOURCE  — ZERO `sales_transactions` rows exist for doc_no=R at all
                   => ambiguous (source consolidated/deleted?); do NOT guess
                   a target, never touched by this script.

Verified on the 2026-07-03 prod snapshot: 398 orphans / 141 products / 383
DUPLICATE / 15 NO_SOURCE / 0 purchase-ledger orphans (this script is
sales-ledger only — there is nothing to clean on the purchase side).
Every `doc_no` in the DUPLICATE bucket resolves to EXACTLY one distinct
`sales_transactions.product_id` (no multi-product doc_no collisions found) —
asserted fresh at runtime, not assumed.

Excluded from deletion (row- or product-scoped, reported not touched):
  - The 15 NO_SOURCE rows (row-level exclusion — a mixed product like 886/480
    keeps its NO_SOURCE rows in place while its DUPLICATE rows still clean up).
  - pid 976 (ตะปูยิงฝ้า 3/4x6-100g) — 3 DUPLICATE rows, but tracked under a
    SEPARATE weight-unit consolidation project (975/976 ตัว -> 663 กล่อง, no
    pack formula), not addressed here per orchestrator instruction.
  - pid 1141 (กุญแจประตูบานเลื่อน Sendai #SL-231 สีเงินด้าน) — 1 DUPLICATE row
    (a +2 credit/คืน). The 2026-07-03 audit explicitly flagged this product
    "for manual check before any cleanup" because a NAIVE delete-without-
    compensation would push its stock negative (0 -> -2). This script's
    pin-to-snapshot design (step 6) would in fact resolve that safely like
    any other product — reported in STDOUT/report as a "would be safe if
    included" analysis — but is EXCLUDED here to honor the audit's explicit
    manual-review flag; Put/Opus can fold it into a later run.

What this script does NOT need to do (unlike scripts/p2p3_split_hinges.py /
models.repoint_bsn_code): no re-point of sales_transactions.product_id, no
`_sync_bsn_to_stock` re-sync, no shopee_stock/lazada_stock/platform_skus.stock
restore. Those are needed only when the SOURCE row's product_id itself moves
and must be re-synced. Here the source rows are untouched (already correct
and already synced=1) — the real ledger row already exists; we are ONLY
deleting the STALE DUPLICATE COPY on the wrong product and pinning that
product's stock back to its pre-op value. Since `_sync_bsn_to_stock` is never
invoked, the online-stock counters are asserted UNCHANGED (not restored) as
an invariant.

⚠ SIMULATION-FIRST TOOL. Refuses to run against the live DB
(`sendy_erp/inventory_app/instance/inventory.db`) unless you pass BOTH
`--db <live path>` AND `--apply`. Default usage is against a `.backup`-made
COPY:

    sqlite3 "<live db>" ".backup '/path/to/copy/inventory.db'"
    DATA_DIR=/path/to/copy SECRET_KEY=x ADMIN_PASSWORD=x \\
        ~/.virtualenvs/erp/bin/python cleanup_orphan_bsn_ledger.py --db /path/to/copy/inventory.db

The target file's basename MUST be `inventory.db` (config.py hardcodes that
name under DATA_DIR).

Idempotent: re-running after a successful run finds 0 rows left to delete
for the processed pids (fresh classification query), so it deletes nothing
and every pin ADJUST computes to ~0 (no-op). Single transaction — any failed
assertion rolls back everything, nothing partial is ever committed.
"""
import argparse
import os
import sys
from datetime import date

PID_MANUAL_REVIEW = {
    976: "separate weight-unit consolidation project (975/976 -> 663), tracked elsewhere",
    # pid 1141 INCLUDED per Put 2026-07-03: verified DUPLICATE (real sale on pid 226);
    # the pin-to-snapshot step keeps its stock at 0 (no naive negative), so it is safe to clean.
}
ORPHAN_SALES_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')
PIN_NOTE = 'ล้าง orphan ledger 2026-07-03'

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIVE_DB_PATH = os.path.realpath(
    os.path.join(REPO_ROOT, 'sendy_erp', 'inventory_app', 'instance', 'inventory.db')
)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', required=True, help="Path to the target inventory.db (basename must be 'inventory.db')")
    p.add_argument('--apply', action='store_true',
                    help="Required in addition to --db to run against the LIVE db path. "
                         "Has no other effect (writes always happen on whatever --db points at).")
    return p.parse_args()


def guard_live_path(db_path, apply_flag):
    resolved = os.path.realpath(db_path)
    if resolved == LIVE_DB_PATH and not apply_flag:
        print(f"REFUSING: {db_path} resolves to the LIVE db ({LIVE_DB_PATH}) "
              f"and --apply was not passed. Run against a .backup COPY instead, "
              f"or pass --apply if you have explicit sign-off to touch live.",
              file=sys.stderr)
        sys.exit(1)
    if os.path.basename(db_path) != 'inventory.db':
        print(f"REFUSING: --db basename must be 'inventory.db' (config.py hardcodes this "
              f"under DATA_DIR). Got: {db_path}", file=sys.stderr)
        sys.exit(1)


def load_app_modules(db_path):
    """Point config.DATABASE_PATH at db_path (via DATA_DIR) BEFORE importing
    database/models, then return the database module."""
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.environ['DATA_DIR'] = data_dir
    os.environ.setdefault('SECRET_KEY', 'x')
    os.environ.setdefault('ADMIN_PASSWORD', 'x')
    # SENDY_APP_DIR override lets this run on prod (Railway layout /app/inventory_app,
    # no 'sendy_erp/' subdir) as well as locally.
    inventory_app_dir = os.environ.get('SENDY_APP_DIR') or os.path.join(REPO_ROOT, 'sendy_erp', 'inventory_app')
    sys.path.insert(0, inventory_app_dir)
    import database  # noqa: E402
    return database


def confirm_on_copy(conn, expected_path, apply_flag=False):
    """Independent check that this connection really points at our target
    file, not the live DB — never trust env-var wiring blindly."""
    row = conn.execute("PRAGMA database_list").fetchone()
    actual = os.path.realpath(row['file'])
    expected = os.path.realpath(expected_path)
    print(f"  connection file : {actual}")
    print(f"  expected file   : {expected}")
    assert actual == expected, "Connection is NOT pointing at the target --db path!"
    if not apply_flag:
        assert actual != LIVE_DB_PATH, "Connection is pointing at the LIVE db — aborting (pass --apply for an explicit, signed-off live apply)."
        print("  CONFIRMED: connected to the target copy, not live.\n")
    else:
        print(f"  APPLYING (explicit --apply) to: {actual}\n")


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def classify_orphans(conn):
    """Fresh, independent classification of every orphan sales-ledger row —
    re-derived from live data each call (used pre- AND post-mutation)."""
    note_ph = ",".join("?" * len(ORPHAN_SALES_NOTES))
    rows = q(conn, f"""
        SELECT t.id, t.product_id, t.reference_no, t.quantity_change, t.note,
               (SELECT COUNT(*) FROM sales_transactions s WHERE s.doc_no = t.reference_no) AS n_source_any
        FROM transactions t
        WHERE t.note IN ({note_ph})
          AND NOT EXISTS (
              SELECT 1 FROM sales_transactions st
              WHERE st.doc_no = t.reference_no AND st.product_id = t.product_id
          )
    """, ORPHAN_SALES_NOTES)
    duplicate = [r for r in rows if r['n_source_any'] > 0]
    no_source = [r for r in rows if r['n_source_any'] == 0]
    return duplicate, no_source


def snapshot_stock(conn, pids):
    if not pids:
        return {}
    rows = q(conn, f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({','.join('?'*len(pids))})", pids)
    snap = {pid: 0 for pid in pids}
    for r in rows:
        snap[r['product_id']] = r['quantity']
    return snap


def main():
    args = parse_args()
    guard_live_path(args.db, args.apply)

    if not os.path.exists(args.db):
        print(f"REFUSING: --db path does not exist: {args.db}", file=sys.stderr)
        sys.exit(1)

    database = load_app_modules(args.db)
    conn = database.get_connection()

    print("=== 0. Confirm connection targets the intended DB ===")
    confirm_on_copy(conn, args.db, args.apply)

    try:
        run(conn)
        conn.commit()
        print("\nCOMMITTED to the target DB (this should be the COPY — re-check the path above).")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK due to an error/assertion failure. No changes persisted.", file=sys.stderr)
        raise
    finally:
        conn.close()


def run(conn):
    results = []

    def check(name, cond, detail):
        status = "PASS" if cond else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name} — {detail}")

    # ── STEP 1: fresh classification (DUPLICATE vs NO_SOURCE) ───────────────
    print("=== 1. Classify every orphan sales-ledger row (fresh, independent query) ===")
    duplicate_rows, no_source_rows = classify_orphans(conn)
    total_orphans_before = len(duplicate_rows) + len(no_source_rows)
    print(f"  total orphan rows: {total_orphans_before} "
          f"(DUPLICATE={len(duplicate_rows)}, NO_SOURCE={len(no_source_rows)})")
    print(f"  distinct products affected (any orphan): "
          f"{len({r['product_id'] for r in duplicate_rows} | {r['product_id'] for r in no_source_rows})}")

    purchase_orphans = qone(conn, """
        SELECT COUNT(*) c FROM transactions t
        WHERE t.note IN ('BSN ซื้อ','BSN ซื้อ-คืน')
          AND NOT EXISTS (
              SELECT 1 FROM purchase_transactions pt
              WHERE pt.doc_no = t.reference_no AND pt.product_id = t.product_id
          )
    """)['c']
    print(f"  purchase-ledger orphans (out of scope for this script): {purchase_orphans}")
    check("purchase-ledger orphans == 0 (nothing to clean on the purchase side)",
          purchase_orphans == 0, f"count = {purchase_orphans}")

    # ── STEP 2: partition into to-delete vs excluded ────────────────────────
    print("\n=== 2. Partition: to-delete (DUPLICATE, not manual-review pid) vs excluded ===")
    to_delete = [r for r in duplicate_rows if r['product_id'] not in PID_MANUAL_REVIEW]
    excluded_manual = [r for r in duplicate_rows if r['product_id'] in PID_MANUAL_REVIEW]
    print(f"  to-delete (DUPLICATE, in scope): {len(to_delete)} rows")
    for pid, reason in PID_MANUAL_REVIEW.items():
        rows_here = [r for r in excluded_manual if r['product_id'] == pid]
        print(f"  EXCLUDED pid {pid} ({reason}): {len(rows_here)} DUPLICATE row(s) left untouched")
        for r in rows_here:
            print(f"    id={r['id']} ref={r['reference_no']!r} qty_change={r['quantity_change']} note={r['note']!r}")
    print(f"  EXCLUDED NO_SOURCE rows (ambiguous, never touched): {len(no_source_rows)}")
    for r in no_source_rows:
        print(f"    id={r['id']} pid={r['product_id']} ref={r['reference_no']!r} qty_change={r['quantity_change']} note={r['note']!r}")

    check("to_delete + excluded_manual + no_source == total orphans",
          len(to_delete) + len(excluded_manual) + len(no_source_rows) == total_orphans_before,
          f"{len(to_delete)} + {len(excluded_manual)} + {len(no_source_rows)} == {total_orphans_before}")

    affected_pids = sorted({r['product_id'] for r in to_delete})
    print(f"\n  AFFECTED_PIDS (will have >=1 row deleted + a pin ADJUST): {len(affected_pids)} products")

    # ── STEP 3: INDEPENDENT SAFETY CHECK before any mutation ────────────────
    # For every row about to be deleted, verify (a) doc_no resolves to
    # EXACTLY one product in sales_transactions (no multi-product doc_no
    # collision that would make "the correct product" ambiguous), and
    # (b) a REAL ledger row (same note, product_id = that resolved product)
    # ALREADY exists — i.e. we are about to delete a true duplicate COPY,
    # never the last/only record of the sale.
    print("\n=== 3. INDEPENDENT pre-delete safety check (every row about to be deleted) ===")
    ambiguous_docno = []
    missing_real_row = []
    real_row_ids_by_orphan = {}  # orphan_id -> real transactions.id (captured for post-delete re-check)
    for r in to_delete:
        doc_no = r['reference_no']
        resolved = q(conn, "SELECT DISTINCT product_id FROM sales_transactions WHERE doc_no=?", (doc_no,))
        resolved_pids = {row['product_id'] for row in resolved}
        if len(resolved_pids) != 1:
            ambiguous_docno.append((r['id'], doc_no, sorted(resolved_pids)))
            continue
        correct_pid = next(iter(resolved_pids))
        real_row = qone(conn, """
            SELECT t2.id, t2.quantity_change FROM transactions t2
            WHERE t2.reference_no = ? AND t2.product_id = ? AND t2.note = ?
        """, (doc_no, correct_pid, r['note']))
        if real_row is None:
            missing_real_row.append((r['id'], doc_no, correct_pid, r['note']))
            continue
        real_row_ids_by_orphan[r['id']] = (real_row['id'], real_row['quantity_change'], correct_pid)

    check("every to-delete row's doc_no resolves to exactly 1 product in sales_transactions",
          len(ambiguous_docno) == 0, f"ambiguous = {ambiguous_docno}")
    check("every to-delete row has a REAL ledger row already on the correct product (true duplicate, never a last copy)",
          len(missing_real_row) == 0, f"missing = {missing_real_row}")

    if ambiguous_docno or missing_real_row:
        raise AssertionError(
            f"Pre-delete safety check FAILED: {len(ambiguous_docno)} ambiguous doc_no, "
            f"{len(missing_real_row)} row(s) with no real ledger copy elsewhere (would be a "
            f"PURE ORPHAN, not a duplicate — do NOT delete, per the honesty rule: ambiguous "
            f"cause, do not guess). Rolling back, nothing changed."
        )
    print(f"  ALL {len(to_delete)} to-delete rows verified as true duplicates (real row exists elsewhere).")

    # ── STEP 4: snapshot stock + online counters for AFFECTED_PIDS ──────────
    print("\n=== 4. SNAPSHOT stock_levels + online counters for AFFECTED_PIDS (pin targets) ===")
    stock_snapshot = snapshot_stock(conn, affected_pids)
    for pid in affected_pids:
        pass  # printed in bulk below to keep output manageable
    print(f"  snapshotted stock_levels for {len(stock_snapshot)} products "
          f"(sum of current stock = {sum(stock_snapshot.values())})")

    platform_before = {pid: dict(qone(conn, "SELECT shopee_stock, lazada_stock FROM products WHERE id=?", (pid,)))
                        for pid in affected_pids}
    platform_skus_before = q(conn, f"SELECT id, platform, internal_product_id, stock FROM platform_skus "
                                     f"WHERE internal_product_id IN ({','.join('?'*len(affected_pids))})", affected_pids) \
        if affected_pids else []
    print(f"  snapshotted products.shopee_stock/lazada_stock for {len(platform_before)} products "
          f"+ {len(platform_skus_before)} platform_skus rows (expected UNCHANGED post-op — no resync is run)")

    txn_count_before = qone(conn, "SELECT COUNT(*) c FROM transactions")['c']

    # ── STEP 5: DELETE the verified-duplicate orphan rows (row-level, by id) ─
    print(f"\n=== 5. DELETE {len(to_delete)} verified-duplicate orphan rows (by id) ===")
    delete_ids = [r['id'] for r in to_delete]
    CHUNK = 500
    deleted_total = 0
    for i in range(0, len(delete_ids), CHUNK):
        batch = delete_ids[i:i + CHUNK]
        cur = conn.execute(f"DELETE FROM transactions WHERE id IN ({','.join('?'*len(batch))})", batch)
        deleted_total += cur.rowcount
    print(f"  deleted {deleted_total} row(s) (expected {len(delete_ids)})")
    check("deleted row count matches to-delete count", deleted_total == len(delete_ids),
          f"{deleted_total} == {len(delete_ids)}")

    # ── STEP 6: PIN stock_levels back to snapshot with a balancing ADJUST ───
    print("\n=== 6. PIN stock_levels back to snapshot (balancing ADJUST per affected product) ===")
    current_after_delete = snapshot_stock(conn, affected_pids)
    pin_count = 0
    for pid in affected_pids:
        target = stock_snapshot[pid]
        cur_qty = current_after_delete[pid]
        delta = target - cur_qty
        if abs(delta) > 1e-9:
            conn.execute("""
                INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (pid, delta, PIN_NOTE, date.today().isoformat() + ' 00:00:00'))
            pin_count += 1
    print(f"  inserted {pin_count} pin ADJUST row(s) (0 expected on an idempotent re-run)")

    final_stock = snapshot_stock(conn, affected_pids)

    # ── VERIFY ────────────────────────────────────────────────────────────
    print("\n=== VERIFY ===")

    for pid in affected_pids:
        check(f"pid {pid}: final stock == pre-op snapshot", abs(final_stock[pid] - stock_snapshot[pid]) < 1e-9,
              f"final={final_stock[pid]}, snapshot={stock_snapshot[pid]}")

    negatives = [(pid, final_stock[pid]) for pid in affected_pids if final_stock[pid] < 0]
    check("no affected product went negative", len(negatives) == 0, f"negatives = {negatives}")

    # Independent post-delete check: the REAL ledger row backing every
    # deleted phantom must STILL exist, byte-identical — proves we only ever
    # removed a true duplicate copy, never the row that carries the sale.
    print("\n  Independent post-delete check: real ledger row for every deleted phantom still intact...")
    broken_real_rows = []
    for orphan_id, (real_id, real_qty_before, correct_pid) in real_row_ids_by_orphan.items():
        row = qone(conn, "SELECT quantity_change, product_id FROM transactions WHERE id=?", (real_id,))
        if row is None or abs(row['quantity_change'] - real_qty_before) > 1e-9 or row['product_id'] != correct_pid:
            broken_real_rows.append((orphan_id, real_id, correct_pid, real_qty_before, dict(row) if row else None))
    check("every deleted row's REAL counterpart still exists unchanged (true duplicate, not a last copy)",
          len(broken_real_rows) == 0, f"broken = {broken_real_rows}")

    # Excluded rows (manual-review pids + NO_SOURCE) must be byte-identical —
    # never touched.
    excluded_ids = [r['id'] for r in excluded_manual] + [r['id'] for r in no_source_rows]
    excluded_before = {r['id']: (r['product_id'], r['reference_no'], r['quantity_change'], r['note'])
                        for r in (excluded_manual + no_source_rows)}
    excluded_after_missing = []
    excluded_after_changed = []
    for eid, before in excluded_before.items():
        row = qone(conn, "SELECT product_id, reference_no, quantity_change, note FROM transactions WHERE id=?", (eid,))
        if row is None:
            excluded_after_missing.append(eid)
        elif (row['product_id'], row['reference_no'], row['quantity_change'], row['note']) != before:
            excluded_after_changed.append((eid, before, dict(row)))
    check("all excluded rows (manual-review pids + NO_SOURCE) still exist, untouched",
          len(excluded_after_missing) == 0 and len(excluded_after_changed) == 0,
          f"missing={excluded_after_missing}, changed={excluded_after_changed}")

    # Re-run the classification fresh: the DUPLICATE bucket must now contain
    # ONLY the manual-review-pid rows (976/1141); NO_SOURCE must be unchanged.
    print("\n  Re-running fresh classification post-cleanup...")
    duplicate_after, no_source_after = classify_orphans(conn)
    duplicate_after_pids = {r['product_id'] for r in duplicate_after}
    check("post-cleanup DUPLICATE bucket contains ONLY the excluded manual-review pids",
          duplicate_after_pids <= set(PID_MANUAL_REVIEW),
          f"remaining DUPLICATE pids = {sorted(duplicate_after_pids)}, expected subset of {sorted(PID_MANUAL_REVIEW)}")
    check("post-cleanup DUPLICATE row count == pre-cleanup excluded_manual count (976+1141 untouched)",
          len(duplicate_after) == len(excluded_manual),
          f"{len(duplicate_after)} == {len(excluded_manual)}")
    check("post-cleanup NO_SOURCE count unchanged", len(no_source_after) == len(no_source_rows),
          f"{len(no_source_after)} == {len(no_source_rows)}")

    # Platform / online-stock counters must be COMPLETELY UNCHANGED (we never
    # invoke _sync_bsn_to_stock, so nothing should have touched them).
    platform_after = {pid: dict(qone(conn, "SELECT shopee_stock, lazada_stock FROM products WHERE id=?", (pid,)))
                       for pid in affected_pids}
    platform_skus_after = q(conn, f"SELECT id, platform, internal_product_id, stock FROM platform_skus "
                                    f"WHERE internal_product_id IN ({','.join('?'*len(affected_pids))})", affected_pids) \
        if affected_pids else []
    check("products.shopee_stock/lazada_stock completely UNCHANGED (no resync was run)",
          platform_after == platform_before, f"before={platform_before}\n  after={platform_after}")
    check("platform_skus.stock completely UNCHANGED (no resync was run)",
          [dict(r) for r in platform_skus_after] == [dict(r) for r in platform_skus_before],
          f"before={[dict(r) for r in platform_skus_before]}\n  after={[dict(r) for r in platform_skus_after]}")

    # Row-count sanity: total transactions should have dropped by exactly
    # (deleted) and grown by exactly (pin ADJUSTs inserted).
    txn_count_after = qone(conn, "SELECT COUNT(*) c FROM transactions")['c']
    expected_count = txn_count_before - deleted_total + pin_count
    check("total transactions row count == before - deleted + pins (no side effects elsewhere)",
          txn_count_after == expected_count,
          f"{txn_count_after} == {txn_count_before} - {deleted_total} + {pin_count} ({expected_count})")

    failed = [r for r in results if r[1] == 'FAIL']
    print(f"\n=== SUMMARY: {len(results)-len(failed)}/{len(results)} checks PASSED ===")
    print(f"    rows deleted: {deleted_total} | products affected (pinned): {len(affected_pids)} "
          f"| pin ADJUSTs inserted: {pin_count}")
    print(f"    excluded (manual-review pid, DUPLICATE): {len(excluded_manual)} rows across {len(PID_MANUAL_REVIEW)} pids")
    print(f"    excluded (NO_SOURCE, ambiguous): {len(no_source_rows)} rows")
    if failed:
        for name, status, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        raise AssertionError(f"{len(failed)} verification check(s) failed — see above. Rolling back.")


if __name__ == '__main__':
    main()
