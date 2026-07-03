#!/usr/bin/env python3
"""
Targeted cleanup of the 14 NO_SOURCE orphan BSN sales-ledger rows the
2026-07-03 DB-wide audit held OUT of scripts/cleanup_orphan_bsn_ledger.py
(that script only cleans DUPLICATE orphans; NO_SOURCE = doc_no absent from
sales_transactions ENTIRELY, never auto-guessed a target).

Per-row adjudication by the data-quality agent (2026-07-03), scope + policy
approved by Put: clean the 11 CLEAN rows + the 3 pid 1211 (ค่าขนส่ง) freight
phantoms. pid 121 (paired-ADJUST) held for separate review; pid 976 handled
under the 975/976->663 consolidation.

STOCK POLICY = MODE A (MIXED), post-scrutinize (finding #1):
  * 193 / 480 / 487  -> CORRECT: delete the phantom rows, DO NOT pin. These
    phantoms are duplicate returns that double-counted stock, so the natural
    post-delete value IS the right one (e.g. 487 192 -> 96). Pinning here
    would MANUFACTURE an ADJUST asserting a known-wrong number.
  * 886 / 1211       -> PRESERVE: delete + pin back to current displayed
    stock. 886's deductions have no traceable invoice (mechanism unclear ->
    don't guess +40; recount physically). 1211 is a freight pseudo-product
    whose "stock" is meaningless.

Intended outcome is pinned as an INVARIANT (EXPECTED_BEFORE / EXPECTED_FINAL):
the run ABORTS if prod stock has drifted from the 2026-07-03 snapshot the
adjudication was based on (fail-safe -> re-adjudicate, never corrupt).

Money note: these rows live ONLY in `transactions` (stock ledger). Freight
income lives in `sales_transactions` (untouched) -> asserted (row count
unchanged). Deleting stock rows does NOT touch the ฿1,450 freight income.

Safety (independent re-derivation, never trust the id list blindly): for
EVERY target id, before deleting, assert (a) the row exists with the expected
product_id and a BSN-sales note, and (b) its reference_no has ZERO exact
matches in sales_transactions (truly NO_SOURCE, not a live sale). Any
mismatch aborts the whole transaction.

Simulation-first: refuses the live DB unless BOTH --db <live> AND --apply.
Single transaction; any failed assertion rolls back everything. Idempotent:
a re-run finds 0 target ids present and is a full no-op.
"""
import argparse
import os
import sys
from datetime import date

# id -> expected product_id. 11 approved CLEAN + 3 pid 1211 freight-phantom.
TARGET_IDS = {
    # --- 11 CLEAN (phantom duplicate return / bogus deduction) ---
    270988: 193,   # ลูกบิด #5112 — phantom SR6700121-2
    245179: 480,   # สายเอ็น #60 ใส — phantom SR6700049-5
    244849: 487,   # สายเอ็น #60 สะท้อนแสง — phantom SR6700049-6
    244850: 487,   # สายเอ็น #60 สะท้อนแสง — phantom SR6700049-7
    270996: 886,   # กุญแจแขวนคอยาว 50mm — deduction, no backing invoice line
    270997: 886,
    270998: 886,
    270999: 886,
    271000: 886,
    245119: 1211,  # ค่าขนส่ง — phantom SR6700163-16 (real return is -15)
    245273: 1211,  # ค่าขนส่ง — phantom SR6700078-2 (real return is -1)
    # --- 3 pid 1211 freight-phantom (Put-approved 2026-07-03) ---
    245270: 1211,  # ค่าขนส่ง — OUT -1 vs IV6703039-2 (doc has only -1, ลูกบิด, no freight)
    245271: 1211,  # ค่าขนส่ง — OUT -1 vs IV6800633-2 (doc has only -1, กุญแจบานเลื่อน)
    245272: 1211,  # ค่าขนส่ง — OUT -1 vs IV6802322-2 (doc has only -1, ถุงหูหิ้ว)
}
ORPHAN_SALES_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')
PIN_NOTE = 'ล้าง NO_SOURCE orphan ledger 2026-07-03'

# MODE A policy: pin (preserve) ONLY these; the rest are corrected (no pin).
PIN_PIDS = {886, 1211}

# Intended-outcome invariant — aborts if prod drifted from the 2026-07-03 snapshot.
EXPECTED_BEFORE = {193: 406, 480: 1446, 487: 192, 886: 0, 1211: 2}
EXPECTED_FINAL = {193: 405, 480: 1440, 487: 96, 886: 0, 1211: 2}

# LIVE path: prefer SENDY_APP_DIR (prod/railway layout) else the local repo layout.
_APP_DIR = os.environ.get('SENDY_APP_DIR')
if _APP_DIR:
    LIVE_DB_PATH = os.path.realpath(os.path.join(_APP_DIR, 'instance', 'inventory.db'))
else:
    _REPO_ROOT = os.path.expanduser('~/Sendai-Boonsawat')
    LIVE_DB_PATH = os.path.realpath(
        os.path.join(_REPO_ROOT, 'sendy_erp', 'inventory_app', 'instance', 'inventory.db')
    )


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', required=True, help="Target inventory.db (basename must be 'inventory.db')")
    p.add_argument('--apply', action='store_true', help="Required to run against the LIVE db path.")
    return p.parse_args()


def guard_live_path(db_path, apply_flag):
    resolved = os.path.realpath(db_path)
    if resolved == LIVE_DB_PATH and not apply_flag:
        print(f"REFUSING: {db_path} resolves to LIVE ({LIVE_DB_PATH}) and --apply not passed. "
              f"Run against a .backup COPY.", file=sys.stderr)
        sys.exit(1)
    if os.path.basename(db_path) != 'inventory.db':
        print(f"REFUSING: --db basename must be 'inventory.db'. Got: {db_path}", file=sys.stderr)
        sys.exit(1)


def load_db_module(db_path):
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.environ['DATA_DIR'] = data_dir
    os.environ.setdefault('SECRET_KEY', 'x')
    os.environ.setdefault('ADMIN_PASSWORD', 'x')
    app_dir = os.environ.get('SENDY_APP_DIR') or os.path.expanduser(
        '~/Sendai-Boonsawat/sendy_erp/inventory_app')
    sys.path.insert(0, app_dir)
    import database
    return database


def confirm_target(conn, expected_path, apply_flag):
    row = conn.execute("PRAGMA database_list").fetchone()
    actual = os.path.realpath(row['file'])
    expected = os.path.realpath(expected_path)
    print(f"  connection file : {actual}")
    print(f"  expected file   : {expected}")
    assert actual == expected, "Connection is NOT pointing at the target --db path!"
    if not apply_flag:
        assert actual != LIVE_DB_PATH, "Connection points at LIVE without --apply — aborting."
        print("  CONFIRMED: target copy, not live.\n")
    else:
        print(f"  APPLYING (--apply) to: {actual}\n")


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def snapshot_stock(conn, pids):
    rows = conn.execute(
        f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({','.join('?'*len(pids))})",
        pids).fetchall()
    snap = {pid: 0 for pid in pids}
    for r in rows:
        snap[r['product_id']] = r['quantity']
    return snap


def main():
    args = parse_args()
    guard_live_path(args.db, args.apply)
    if not os.path.exists(args.db):
        print(f"REFUSING: --db does not exist: {args.db}", file=sys.stderr)
        sys.exit(1)

    database = load_db_module(args.db)
    conn = database.get_connection()
    print("=== 0. Confirm connection targets the intended DB ===")
    confirm_target(conn, args.db, args.apply)

    try:
        run(conn)
        conn.commit()
        print("\nCOMMITTED to the target DB (verify the path above is the COPY unless --apply).")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK — no changes persisted.", file=sys.stderr)
        raise
    finally:
        conn.close()


def run(conn):
    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    affected_pids = sorted(set(TARGET_IDS.values()))

    # STEP 1: independent re-validation of every target id ---------------------
    print("=== 1. Independent re-validation of each target id (present, NO_SOURCE, expected pid) ===")
    present, missing, wrong_pid, wrong_note, not_no_source = [], [], [], [], []
    for tid, expected_pid in TARGET_IDS.items():
        row = one(conn, "SELECT id, product_id, reference_no, quantity_change, note "
                        "FROM transactions WHERE id=?", (tid,))
        if row is None:
            missing.append(tid)
            continue
        present.append(tid)
        if row['product_id'] != expected_pid:
            wrong_pid.append((tid, row['product_id'], expected_pid))
        if row['note'] not in ORPHAN_SALES_NOTES:
            wrong_note.append((tid, row['note']))
        n_src = one(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE doc_no=?",
                    (row['reference_no'],))['c']
        if n_src != 0:
            not_no_source.append((tid, row['reference_no'], n_src))
    print(f"  present now: {len(present)} / {len(TARGET_IDS)}   (missing = already-deleted on a re-run: {missing})")
    check("no present target has a wrong product_id", not wrong_pid, str(wrong_pid))
    check("every present target carries a BSN-sales note", not wrong_note, str(wrong_note))
    check("every present target is truly NO_SOURCE (0 sales_transactions for its ref)",
          not not_no_source, str(not_no_source))
    if wrong_pid or wrong_note or not_no_source:
        raise AssertionError("Target re-validation FAILED — rolling back, nothing changed.")

    if not present:
        print("\n  Nothing to delete (all targets already gone). Idempotent no-op.")
        _finalize(results)
        return

    # STEP 2: snapshot stock + assert it matches the adjudicated snapshot -------
    print("\n=== 2. Snapshot stock_levels + assert EXPECTED_BEFORE (drift guard) + sales_transactions count ===")
    stock_before = snapshot_stock(conn, affected_pids)
    st_count_before = one(conn, "SELECT COUNT(*) c FROM sales_transactions")['c']
    txn_count_before = one(conn, "SELECT COUNT(*) c FROM transactions")['c']
    for pid in affected_pids:
        print(f"    pid {pid}: stock {stock_before[pid]}  (expected before {EXPECTED_BEFORE[pid]})")
    drift = {pid: stock_before[pid] for pid in affected_pids if stock_before[pid] != EXPECTED_BEFORE[pid]}
    check("current stock == EXPECTED_BEFORE (no prod drift since 2026-07-03 adjudication)",
          not drift, f"drift = {drift}")
    if drift:
        raise AssertionError(
            f"Stock drifted from the adjudicated snapshot: {drift}. The 96/40/etc. targets were "
            f"derived from EXPECTED_BEFORE — re-adjudicate before applying. Rolling back.")
    print(f"  sales_transactions rows (must be UNCHANGED post-op): {st_count_before}")

    # STEP 3: delete the present target rows -----------------------------------
    print(f"\n=== 3. DELETE {len(present)} target rows (by id) ===")
    cur = conn.execute(
        f"DELETE FROM transactions WHERE id IN ({','.join('?'*len(present))})", present)
    print(f"  deleted {cur.rowcount} (expected {len(present)})")
    check("deleted count == present target count", cur.rowcount == len(present),
          f"{cur.rowcount} == {len(present)}")

    # STEP 4: MODE A — pin only PIN_PIDS (preserve); others keep corrected value -
    print("\n=== 4. MODE A: pin PRESERVE pids {886,1211}; CORRECT the rest (no pin) ===")
    after_delete = snapshot_stock(conn, affected_pids)
    pins = 0
    for pid in affected_pids:
        if pid in PIN_PIDS:
            delta = stock_before[pid] - after_delete[pid]
            if abs(delta) > 1e-9:
                conn.execute(
                    "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
                    "reference_no, note, created_at) VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)",
                    (pid, delta, PIN_NOTE, date.today().isoformat() + ' 00:00:00'))
                pins += 1
                print(f"    pid {pid}: PIN ADJUST {delta:+g} -> preserve {stock_before[pid]}")
            else:
                print(f"    pid {pid}: PIN pid, but delta 0 (no delete affected it)")
        else:
            print(f"    pid {pid}: CORRECT (no pin) {stock_before[pid]} -> {after_delete[pid]}")

    # STEP 5: verify -----------------------------------------------------------
    print("\n=== 5. VERIFY (against EXPECTED_FINAL + policy) ===")
    final = snapshot_stock(conn, affected_pids)
    for pid in affected_pids:
        check(f"pid {pid}: final == EXPECTED_FINAL", abs(final[pid] - EXPECTED_FINAL[pid]) < 1e-9,
              f"{final[pid]} == {EXPECTED_FINAL[pid]}")
        if pid in PIN_PIDS:
            check(f"pid {pid}: preserved (final == before)", abs(final[pid] - stock_before[pid]) < 1e-9,
                  f"{final[pid]} == {stock_before[pid]}")
        else:
            check(f"pid {pid}: corrected (final == after_delete, no pin)",
                  abs(final[pid] - after_delete[pid]) < 1e-9, f"{final[pid]} == {after_delete[pid]}")
    check("no affected product negative", all(final[p] >= 0 for p in affected_pids),
          str({p: final[p] for p in affected_pids if final[p] < 0}))
    st_count_after = one(conn, "SELECT COUNT(*) c FROM sales_transactions")['c']
    check("sales_transactions COMPLETELY UNCHANGED (freight income untouched)",
          st_count_after == st_count_before, f"{st_count_after} == {st_count_before}")
    txn_count_after = one(conn, "SELECT COUNT(*) c FROM transactions")['c']
    expected_txn = txn_count_before - len(present) + pins
    check("transactions count == before - deleted + pins (no side effects)",
          txn_count_after == expected_txn, f"{txn_count_after} == {expected_txn}")
    still_there = [t for t in present if one(conn, "SELECT 1 FROM transactions WHERE id=?", (t,))]
    check("every deleted target id is gone", not still_there, str(still_there))

    _finalize(results, len(present), pins)


def _finalize(results, deleted=0, pins=0):
    failed = [n for n, ok in results if not ok]
    print(f"\n=== SUMMARY: {len(results)-len(failed)}/{len(results)} checks PASSED "
          f"| deleted={deleted} | pins={pins} ===")
    if failed:
        raise AssertionError(f"{len(failed)} check(s) failed: {failed} — rolling back.")


if __name__ == '__main__':
    main()
