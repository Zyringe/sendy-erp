#!/usr/bin/env python3
"""
P0 — split the 3.5in hinge into separate active แผง (pid 98) + ตัว (pid 105) SKUs.

Implements the Reconciliation Procedure (RP, plan §4) from
`projects/pack-loose-sku-split/plan.md`, scoped to Phase 0 (§6):

  - Reactivate pid 98 (แผง), keep pid 105 (ตัว) active.
  - Remap BSN code 030บ5125 (แผง) -> pid 98 (currently mis-mapped to 105).
    Keep 030บ5135 (ตัว) -> pid 105.
  - Remove the bogus `แผง -> 3` unit_conversions row on pid 105 (merge artifact
    from the 2026-06-19 ad-hoc merge script).
  - Re-point sales_transactions / purchase_transactions rows for the two codes,
    delete the stale ledger on {98,105}, re-sync via the REAL
    `models._sync_bsn_to_stock`, then pin final stock_levels back to the
    pre-op snapshot with one balancing ADJUST per product (Put's decision:
    final stock = current stock, preserved, not recounted).
  - Runs every §4.6 invariant as an assertion and prints PASS/FAIL.

⚠ SIMULATION-FIRST TOOL. This script REFUSES to run against the live DB
(`sendy_erp/inventory_app/instance/inventory.db`) unless you pass BOTH
`--db <live path>` AND `--apply`. Default usage is always against a
`.backup`-made COPY:

    sqlite3 "<live db>" ".backup '/path/to/copy/inventory.db'"
    DATA_DIR=/path/to/copy SECRET_KEY=x ADMIN_PASSWORD=x \\
        ~/.virtualenvs/erp/bin/python p0_split_3p5in.py --db /path/to/copy/inventory.db

The target file's basename MUST be `inventory.db` (config.py hardcodes that
name under DATA_DIR).

Re-runnable: if the copy has already been through this script once, all
mapping/remap steps are idempotent (plain UPDATE/DELETE), the ledger DELETE
matches by note pattern (nothing left to delete the 2nd time), and the
re-sync only processes synced_to_stock=0 rows. The final pin ADJUST is
computed fresh each run against whatever the ledger currently nets to, so
re-running after a first successful run should compute an ADJUST of ~0.
"""
import argparse
import os
import sys
from datetime import date

# ── Fixed scope for this phase (see plan.md §6 Phase 0) ─────────────────────
PID_PANEL = 98        # แผง SKU (currently inactive)
PID_LOOSE = 105        # ตัว SKU (active)
CODE_PANEL = '030บ5125'   # BSN code that should route to the แผง SKU
CODE_LOOSE = '030บ5135'   # BSN code that should route to the ตัว SKU
PIN_NOTE = 'ตั้งต้นหลังแยก SKU 2026-07-02'
BSN_LEDGER_DELETE_SQL = (
    "DELETE FROM transactions WHERE product_id IN (?, ?) "
    "AND (note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%')"
)

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
    database/models, then return (database, models) modules."""
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.environ['DATA_DIR'] = data_dir
    os.environ.setdefault('SECRET_KEY', 'x')
    os.environ.setdefault('ADMIN_PASSWORD', 'x')
    # SENDY_APP_DIR override lets this run on prod (Railway layout /app/inventory_app,
    # no 'sendy_erp/' subdir) as well as locally.
    inventory_app_dir = os.environ.get('SENDY_APP_DIR') or os.path.join(REPO_ROOT, 'sendy_erp', 'inventory_app')
    sys.path.insert(0, inventory_app_dir)
    import database  # noqa: E402
    import models    # noqa: E402
    return database, models


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
        # --apply = explicit sign-off to mutate whatever --db points at (the
        # local live DB OR a prod path like /data/inventory.db, where the
        # repo-relative LIVE_DB_PATH is meaningless). actual==expected already
        # asserted above; that's the real safety check.
        print(f"  APPLYING (explicit --apply) to: {actual}\n")


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def snapshot_stock(conn, pids):
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

    database, models = load_app_modules(args.db)
    conn = database.get_connection()

    print("=== 0. Confirm connection targets the intended DB ===")
    confirm_on_copy(conn, args.db, args.apply)

    try:
        run(conn, models)
        conn.commit()
        print("\nCOMMITTED to the target DB (this should be the COPY — re-check the path above).")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK due to an error/assertion failure. No changes persisted.", file=sys.stderr)
        raise
    finally:
        conn.close()


def run(conn, models):
    pids = [PID_PANEL, PID_LOOSE]

    # ── STEP 1: snapshot current stock (RP §4.1) — these are the target
    # final stocks Put decided to pin to (current stock, not a fresh count).
    print("=== 1. SNAPSHOT stock_levels (target final stock) ===")
    snapshot = snapshot_stock(conn, pids)
    for pid in pids:
        print(f"  pid {pid}: snapshot = {snapshot[pid]}")

    before_active = qone(conn, "SELECT is_active FROM products WHERE id=?", (PID_PANEL,))['is_active']
    before_mapping_panel = qone(conn, "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE_PANEL,))
    before_mapping_loose = qone(conn, "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE_LOOSE,))
    before_uc_bogus = qone(conn, "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit='แผง'", (PID_LOOSE,))
    before_bsn_count = {
        pid: qone(conn, "SELECT COUNT(*) c FROM transactions WHERE product_id=? AND note LIKE 'BSN%'", (pid,))['c']
        for pid in pids
    }
    print(f"  pid {PID_PANEL} is_active (before) = {before_active}")
    print(f"  mapping {CODE_PANEL} -> product_id (before) = {before_mapping_panel['product_id'] if before_mapping_panel else None}")
    print(f"  mapping {CODE_LOOSE} -> product_id (before) = {before_mapping_loose['product_id'] if before_mapping_loose else None}")
    print(f"  pid {PID_LOOSE} bogus แผง unit_conversion ratio (before) = {before_uc_bogus['ratio'] if before_uc_bogus else None}")
    print(f"  BSN-note transactions count before: pid98={before_bsn_count[PID_PANEL]}, pid105={before_bsn_count[PID_LOOSE]}")

    # Pre-move row-count totals for the two codes (must be unchanged after re-point).
    pre_sales_panel = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (CODE_PANEL,))['c']
    pre_sales_loose = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (CODE_LOOSE,))['c']
    pre_purch_panel = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE bsn_code=?", (CODE_PANEL,))['c']
    pre_purch_loose = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE bsn_code=?", (CODE_LOOSE,))['c']
    print(f"  pre-move row counts: sales({CODE_PANEL})={pre_sales_panel}, sales({CODE_LOOSE})={pre_sales_loose}, "
          f"purchase({CODE_PANEL})={pre_purch_panel}, purchase({CODE_LOOSE})={pre_purch_loose}")

    # ── STEP 2: reactivate pid 98, remap code, remove bogus conversion ──────
    print("\n=== 2. Reactivate pid 98 + remap code + remove bogus unit_conversion ===")
    print(f"  SQL: UPDATE products SET is_active=1 WHERE id={PID_PANEL}")
    conn.execute("UPDATE products SET is_active=1 WHERE id=?", (PID_PANEL,))

    print(f"  SQL: UPDATE product_code_mapping SET product_id={PID_PANEL} WHERE bsn_code='{CODE_PANEL}'")
    conn.execute("UPDATE product_code_mapping SET product_id=? WHERE bsn_code=?", (PID_PANEL, CODE_PANEL))

    print(f"  SQL: DELETE FROM unit_conversions WHERE product_id={PID_LOOSE} AND bsn_unit='แผง'")
    cur = conn.execute("DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit='แผง'", (PID_LOOSE,))
    print(f"    -> {cur.rowcount} row(s) deleted")

    # ── STEP 3: re-point sales_transactions / purchase_transactions ────────
    print("\n=== 3. Re-point sales_transactions / purchase_transactions ===")
    print(f"  SQL: UPDATE sales_transactions SET product_id={PID_PANEL}, synced_to_stock=0 WHERE bsn_code='{CODE_PANEL}'")
    cur = conn.execute(
        "UPDATE sales_transactions SET product_id=?, synced_to_stock=0 WHERE bsn_code=?",
        (PID_PANEL, CODE_PANEL)
    )
    print(f"    -> {cur.rowcount} row(s) updated")

    print(f"  SQL: UPDATE sales_transactions SET synced_to_stock=0 WHERE bsn_code='{CODE_LOOSE}'  (product stays {PID_LOOSE})")
    cur = conn.execute(
        "UPDATE sales_transactions SET synced_to_stock=0 WHERE bsn_code=?",
        (CODE_LOOSE,)
    )
    print(f"    -> {cur.rowcount} row(s) updated")

    print(f"  SQL: UPDATE purchase_transactions SET synced_to_stock=0 WHERE bsn_code='{CODE_LOOSE}'  (product stays {PID_LOOSE})")
    cur = conn.execute(
        "UPDATE purchase_transactions SET synced_to_stock=0 WHERE bsn_code=?",
        (CODE_LOOSE,)
    )
    print(f"    -> {cur.rowcount} row(s) updated")

    # No purchase rows expected for CODE_PANEL (แผง has no purchase source —
    # แผง comes from ขึ้นแผง pack events, not BSN buys). Reset defensively if any exist.
    cur = conn.execute(
        "UPDATE purchase_transactions SET product_id=?, synced_to_stock=0 WHERE bsn_code=?",
        (PID_PANEL, CODE_PANEL)
    )
    if cur.rowcount:
        print(f"  NOTE: unexpectedly found {cur.rowcount} purchase_transactions row(s) for {CODE_PANEL} — re-pointed to {PID_PANEL}.")

    # ── STEP 4: delete stale ledger for {98,105} ────────────────────────────
    print("\n=== 4. DELETE old ledger for pid 98,105 (BSN%/รวม%/reconcile ledger) ===")
    preview = q(conn,
        "SELECT product_id, note, COUNT(*) c, SUM(quantity_change) s FROM transactions "
        "WHERE product_id IN (?,?) AND (note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%') "
        "GROUP BY product_id, note ORDER BY product_id",
        pids)
    print("  Preview of rows to be deleted:")
    for r in preview:
        print(f"    pid {r['product_id']:>4} | {r['note']:<28} | rows={r['c']:>3} | sum={r['s']}")
    total_to_delete = sum(r['c'] for r in preview)
    print(f"  SQL: {BSN_LEDGER_DELETE_SQL}")
    cur = conn.execute(BSN_LEDGER_DELETE_SQL, pids)
    print(f"    -> {cur.rowcount} row(s) deleted (preview said {total_to_delete})")
    assert cur.rowcount == total_to_delete, "Delete count didn't match preview count!"

    kept = q(conn, "SELECT product_id, note, quantity_change FROM transactions WHERE product_id IN (?,?) ORDER BY product_id", pids)
    print("  Rows KEPT on 98/105 after delete (should be only opening/manual rows):")
    for r in kept:
        print(f"    pid {r['product_id']:>4} | {r['note']:<28} | qty_change={r['quantity_change']}")

    # ── STEP 5: re-sync via the REAL app function ───────────────────────────
    print("\n=== 5. Re-sync via models._sync_bsn_to_stock (sales, then purchase) ===")
    unsynced_sales_before = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    unsynced_purch_before = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    print(f"  (whole-table unsynced rows before: sales={unsynced_sales_before}, purchase={unsynced_purch_before} "
          f"— re-sync processes ALL pending rows, not just our two codes; unrelated rows with no "
          f"unit_conversions ratio are silently skipped by _get_base_qty)")

    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')

    unsynced_sales_after = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    unsynced_purch_after = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    print(f"  whole-table unsynced rows after: sales={unsynced_sales_after}, purchase={unsynced_purch_after}")

    rebuilt = q(conn, "SELECT product_id, COUNT(*) c, SUM(quantity_change) s FROM transactions "
                       "WHERE product_id IN (?,?) AND note LIKE 'BSN%' GROUP BY product_id", pids)
    print("  Rebuilt BSN rows on 98/105:")
    for r in rebuilt:
        print(f"    pid {r['product_id']:>4} | rows={r['c']:>3} | sum={r['s']}")

    # ── STEP 6: pin stock to snapshot with a balancing ADJUST ───────────────
    print("\n=== 6. PIN stock_levels back to snapshot (balancing ADJUST) ===")
    current = snapshot_stock(conn, pids)
    for pid in pids:
        target = snapshot[pid]
        cur_qty = current[pid]
        delta = target - cur_qty
        print(f"  pid {pid}: target={target}, current_after_resync={cur_qty}, delta(ADJUST)={delta}")
        if abs(delta) > 1e-9:
            conn.execute("""
                INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (pid, delta, PIN_NOTE, date.today().isoformat() + ' 00:00:00'))
        else:
            print(f"    -> delta ~0, no ADJUST needed")

    final = snapshot_stock(conn, pids)
    print(f"  FINAL stock_levels: pid98={final[PID_PANEL]}, pid105={final[PID_LOOSE]}")

    # ── VERIFY (§4.6 invariants + the specific reported-bug proof) ─────────
    print("\n=== VERIFY ===")
    results = []

    def check(name, cond, detail):
        status = "PASS" if cond else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name} — {detail}")

    check(
        "pid 98 is_active=1",
        qone(conn, "SELECT is_active FROM products WHERE id=?", (PID_PANEL,))['is_active'] == 1,
        f"is_active = {qone(conn, 'SELECT is_active FROM products WHERE id=?', (PID_PANEL,))['is_active']}"
    )

    iv_row = qone(conn,
        "SELECT product_id, quantity_change, unit_mode FROM transactions WHERE reference_no=? AND note LIKE 'BSN%'",
        ('IV6900357-4',))
    check(
        "IV6900357-4 ledger row on pid 98, qty -12 (not -36)",
        bool(iv_row) and iv_row['product_id'] == PID_PANEL and abs(iv_row['quantity_change'] - (-12)) < 1e-9,
        f"row = {dict(iv_row) if iv_row else None}"
    )

    stray_on_105 = qone(conn,
        "SELECT COUNT(*) c FROM transactions WHERE product_id=? AND reference_no IN "
        "(SELECT doc_no FROM sales_transactions WHERE bsn_code=?)",
        (PID_LOOSE, CODE_PANEL))['c']
    check(
        "pid 105 ledger has NO 5125/แผง rows",
        stray_on_105 == 0,
        f"count = {stray_on_105}"
    )

    check(
        "stock_levels pid98 == snapshot",
        abs(final[PID_PANEL] - snapshot[PID_PANEL]) < 1e-9,
        f"final={final[PID_PANEL]}, snapshot={snapshot[PID_PANEL]}"
    )
    check(
        "stock_levels pid105 == snapshot",
        abs(final[PID_LOOSE] - snapshot[PID_LOOSE]) < 1e-9,
        f"final={final[PID_LOOSE]}, snapshot={snapshot[PID_LOOSE]}"
    )

    dupes = q(conn,
        "SELECT reference_no, product_id, COUNT(*) c FROM transactions "
        "WHERE product_id IN (?,?) AND note LIKE 'BSN%' AND reference_no IS NOT NULL "
        "GROUP BY reference_no, product_id HAVING c > 1", pids)
    check(
        "no duplicate (reference_no, product_id) among BSN rows on 98/105",
        len(dupes) == 0,
        f"dupes = {[dict(d) for d in dupes]}"
    )

    # ratio check: every rebuilt BSN row's |quantity_change| must equal the
    # sales/purchase qty for that doc (ratio 1 on both sides post-fix).
    ratio_mismatches = q(conn, """
        SELECT t.reference_no, t.product_id, t.quantity_change, s.qty, s.unit
        FROM transactions t
        JOIN sales_transactions s ON s.doc_no = t.reference_no
        WHERE t.product_id IN (?,?) AND t.note LIKE 'BSN%'
          AND ABS(ABS(t.quantity_change) - s.qty) > 1e-6
    """, pids)
    check(
        "pid98 rows in แผง / pid105 rows in ตัว, both ratio 1 (no ×3)",
        len(ratio_mismatches) == 0,
        f"mismatches = {[dict(m) for m in ratio_mismatches]}"
    )

    post_sales_panel = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (CODE_PANEL,))['c']
    post_sales_loose = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (CODE_LOOSE,))['c']
    post_purch_loose = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE bsn_code=?", (CODE_LOOSE,))['c']
    check(
        "re-pointed row counts match pre-move totals",
        (post_sales_panel, post_sales_loose, post_purch_loose) == (pre_sales_panel, pre_sales_loose, pre_purch_loose),
        f"before=({pre_sales_panel},{pre_sales_loose},{pre_purch_loose}) after=({post_sales_panel},{post_sales_loose},{post_purch_loose})"
    )

    post_pids_panel = {r['product_id'] for r in q(conn, "SELECT DISTINCT product_id FROM sales_transactions WHERE bsn_code=?", (CODE_PANEL,))}
    post_pids_loose_s = {r['product_id'] for r in q(conn, "SELECT DISTINCT product_id FROM sales_transactions WHERE bsn_code=?", (CODE_LOOSE,))}
    post_pids_loose_p = {r['product_id'] for r in q(conn, "SELECT DISTINCT product_id FROM purchase_transactions WHERE bsn_code=?", (CODE_LOOSE,))}
    check(
        f"{CODE_PANEL} now routes only to pid {PID_PANEL}",
        post_pids_panel == {PID_PANEL},
        f"product_ids seen = {post_pids_panel}"
    )
    check(
        f"{CODE_LOOSE} still routes only to pid {PID_LOOSE} (sales+purchase)",
        post_pids_loose_s <= {PID_LOOSE} and post_pids_loose_p <= {PID_LOOSE},
        f"sales product_ids = {post_pids_loose_s}, purchase product_ids = {post_pids_loose_p}"
    )

    # Negative stock — flag, don't fail (expected & OK per plan §4.6 note).
    negatives = [(pid, final[pid]) for pid in pids if final[pid] < 0]
    if negatives:
        print(f"  [FLAG] negative stock after pin: {negatives} — expected/OK per plan, Put accepts, recount later.")
    else:
        print(f"  [OK] no negative stock among {pids} after pin.")

    failed = [r for r in results if r[1] == 'FAIL']
    print(f"\n=== SUMMARY: {len(results)-len(failed)}/{len(results)} checks PASSED ===")
    if failed:
        for name, status, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        raise AssertionError(f"{len(failed)} verification check(s) failed — see above. Not committing further.")


if __name__ == '__main__':
    main()
