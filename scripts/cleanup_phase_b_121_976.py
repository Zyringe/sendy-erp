#!/usr/bin/env python3
"""
Phase B — the two orphan-ledger cleanups the 2026-07-03 audit HELD for
per-case adjudication (see decisions/log.md 2026-07-03, plan
projects/pack-loose-sku-split/plan.md §9a). Put approved both 2026-07-03.

Two products, two DIFFERENT orphan shapes:

pid 121 (บานพับหน้าต่างกล่องขาว Sendai 12in สีบรอนซ์) — PAIRED DELETE, no pin.
  The product's REAL rows already net to 0 (return +34 SR6700136-1 id 263881
  + sales -34 + opening 0). On top sit two redundant rows that cancel:
    - id 244948: a NO_SOURCE phantom return +34 (fake ref 'SR6700136-2';
      the doc has only a -1 line) — the same phantom-duplicate bug class.
    - id 268949: a manual -34 'ปรับปรุงยอด (reconcile ledger)' ADJUST from the
      2026-05-30 batch, added to cancel the phantom's +34 without removing it.
  Deleting the phantom ALONE would push stock to -34. Delete BOTH -> stock
  stays 0 and the ledger is clean (real rows net 0). No pin (nets to 0).

pid 976 (ตะปูยิงฝ้า 3/4x6-100g, is_active=0, stock 0) — DUPLICATE DELETE + pin.
  The 975/976 -> 663 (กล่อง) weight-unit consolidation already happened at the
  sales/mapping level: all three doc_nos' real sales_transactions lines are on
  pid 663 now (975/976 have zero sales rows, deactivated). The 3 rows here are
  stranded DUPLICATE phantoms on the dead SKU:
    260266 (-1, IV6801404-2), 260267 (-4, IV6801880-1), 264075 (-1, IV6900576-2).
  Real sale is on 663 -> delete the phantoms + pin 976 stock back to 0
  (preserve; it is a dead consolidated SKU, its inventory lives on 663).

Intended outcome pinned as an INVARIANT (EXPECTED_BEFORE / EXPECTED_FINAL,
both {121:0, 976:0}); aborts on any prod drift. Independent per-id
re-validation before delete (phantom is truly NO_SOURCE; the ADJUST is the
-34 reconcile row; each 976 id is truly DUPLICATE with its real row NOT on
976). Simulate-first (refuses live w/o --db + --apply); single transaction;
rollback on any failure; idempotent (re-run finds 0 target ids -> no-op).
Freight/other money untouched — only `transactions` is written;
sales_transactions row count asserted unchanged.
"""
import argparse
import os
import sys
from datetime import date

ORPHAN_SALES_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')
PIN_NOTE = 'ล้าง orphan ledger phase B 2026-07-03'

# pid 121 paired delete: {id: (pid, expected_qty, kind)}
PAIRED_121 = {
    244948: (121, 34, 'phantom'),   # NO_SOURCE phantom return (fake SR6700136-2)
    268949: (121, -34, 'adjust'),   # -34 reconcile ADJUST that cancels it
}
# pid 976 DUPLICATE phantoms (real sale re-pointed to 663): {id: pid}
DUP_976 = {260266: 976, 260267: 976, 264075: 976}

PIN_PIDS = {976}                       # preserve dead-SKU stock at 0
NO_PIN_PIDS = {121}                    # paired delete nets to 0, no pin
EXPECTED_BEFORE = {121: 0, 976: 0}
EXPECTED_FINAL = {121: 0, 976: 0}

_APP_DIR = os.environ.get('SENDY_APP_DIR')
if _APP_DIR:
    LIVE_DB_PATH = os.path.realpath(os.path.join(_APP_DIR, 'instance', 'inventory.db'))
else:
    LIVE_DB_PATH = os.path.realpath(os.path.expanduser(
        '~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db'))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', required=True)
    p.add_argument('--apply', action='store_true')
    return p.parse_args()


def guard_live_path(db_path, apply_flag):
    if os.path.realpath(db_path) == LIVE_DB_PATH and not apply_flag:
        print(f"REFUSING: {db_path} is LIVE and --apply not passed. Use a .backup COPY.", file=sys.stderr)
        sys.exit(1)
    if os.path.basename(db_path) != 'inventory.db':
        print(f"REFUSING: --db basename must be 'inventory.db'. Got: {db_path}", file=sys.stderr)
        sys.exit(1)


def load_db_module(db_path):
    os.environ['DATA_DIR'] = os.path.dirname(os.path.abspath(db_path))
    os.environ.setdefault('SECRET_KEY', 'x')
    os.environ.setdefault('ADMIN_PASSWORD', 'x')
    app_dir = os.environ.get('SENDY_APP_DIR') or os.path.expanduser(
        '~/Sendai-Boonsawat/sendy_erp/inventory_app')
    sys.path.insert(0, app_dir)
    import database
    return database


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
    print("=== 0. Confirm connection ===")
    row = one(conn, "PRAGMA database_list")
    actual = os.path.realpath(row['file'])
    print(f"  connection file: {actual}")
    assert actual == os.path.realpath(args.db), "connection != --db"
    if not args.apply:
        assert actual != LIVE_DB_PATH, "points at LIVE without --apply"
        print("  CONFIRMED target copy, not live.\n")
    else:
        print(f"  APPLYING (--apply) to: {actual}\n")
    try:
        run(conn)
        conn.commit()
        print("\nCOMMITTED to the target DB.")
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

    affected_pids = sorted(set(list(EXPECTED_BEFORE)))
    all_ids = list(PAIRED_121) + list(DUP_976)

    # STEP 1: independent re-validation -----------------------------------------
    print("=== 1. Independent re-validation of every target id ===")
    present, missing, problems = [], [], []
    for tid in all_ids:
        r = one(conn, "SELECT id, product_id, txn_type, quantity_change, reference_no, note "
                      "FROM transactions WHERE id=?", (tid,))
        if r is None:
            missing.append(tid)
            continue
        present.append(tid)

    # pid 121: phantom is NO_SOURCE; ADJUST is the -34 reconcile; pair nets 0.
    for tid, (pid, qty, kind) in PAIRED_121.items():
        if tid in missing:
            continue
        r = one(conn, "SELECT product_id, txn_type, quantity_change, reference_no, note "
                      "FROM transactions WHERE id=?", (tid,))
        if r['product_id'] != pid or r['quantity_change'] != qty:
            problems.append((tid, 'pid/qty mismatch', dict(r)))
        if kind == 'phantom':
            if r['note'] not in ORPHAN_SALES_NOTES:
                problems.append((tid, 'phantom not a BSN-sales note', r['note']))
            n_src = one(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE doc_no=?",
                        (r['reference_no'],))['c']
            if n_src != 0:
                problems.append((tid, 'phantom ref is NOT NO_SOURCE', (r['reference_no'], n_src)))
        elif kind == 'adjust':
            if r['txn_type'] != 'ADJUST' or 'ปรับปรุง' not in (r['note'] or ''):
                problems.append((tid, 'not the reconcile ADJUST', dict(r)))
    paired_present = [t for t in PAIRED_121 if t not in missing]
    if paired_present:
        paired_sum = sum(PAIRED_121[t][1] for t in paired_present)
        check("pid 121 paired rows sum to 0 (safe: stock unchanged by the pair)",
              len(paired_present) == 2 and paired_sum == 0, f"n={len(paired_present)} sum={paired_sum}")

    # pid 976: each is DUPLICATE (doc_no has a real sales row, NONE on 976).
    for tid, pid in DUP_976.items():
        if tid in missing:
            continue
        r = one(conn, "SELECT product_id, quantity_change, reference_no, note FROM transactions WHERE id=?", (tid,))
        if r['product_id'] != pid:
            problems.append((tid, 'not pid 976', r['product_id']))
        if r['note'] not in ORPHAN_SALES_NOTES:
            problems.append((tid, '976 row not a BSN-sales note', r['note']))
        n_any = one(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE doc_no=?", (r['reference_no'],))['c']
        n_self = one(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE doc_no=? AND product_id=976",
                     (r['reference_no'],))['c']
        if not (n_any > 0 and n_self == 0):
            problems.append((tid, 'not a clean DUPLICATE (real sale must exist, none on 976)', (n_any, n_self)))

    print(f"  present: {len(present)}/{len(all_ids)}  (missing/already-clean: {missing})")
    check("no validation problems on any present target", not problems, str(problems))
    if problems:
        raise AssertionError("Re-validation FAILED — rolling back, nothing changed.")

    if not present:
        print("\n  Nothing to delete — idempotent no-op.")
        _finalize(results)
        return

    # STEP 2: snapshot + drift guard --------------------------------------------
    print("\n=== 2. Snapshot + EXPECTED_BEFORE drift guard + sales_transactions count ===")
    stock_before = snapshot_stock(conn, affected_pids)
    st_before = one(conn, "SELECT COUNT(*) c FROM sales_transactions")['c']
    txn_before = one(conn, "SELECT COUNT(*) c FROM transactions")['c']
    for pid in affected_pids:
        print(f"    pid {pid}: stock {stock_before[pid]} (expected {EXPECTED_BEFORE[pid]})")
    drift = {p: stock_before[p] for p in affected_pids if stock_before[p] != EXPECTED_BEFORE[p]}
    check("stock == EXPECTED_BEFORE (no prod drift)", not drift, f"drift={drift}")
    if drift:
        raise AssertionError(f"Drift {drift} — re-adjudicate. Rolling back.")
    print(f"  sales_transactions rows (must stay {st_before})")

    # STEP 3: delete ------------------------------------------------------------
    print(f"\n=== 3. DELETE {len(present)} rows ===")
    cur = conn.execute(f"DELETE FROM transactions WHERE id IN ({','.join('?'*len(present))})", present)
    print(f"  deleted {cur.rowcount} (expected {len(present)})")
    check("deleted == present", cur.rowcount == len(present), f"{cur.rowcount} == {len(present)}")

    # STEP 4: pin PIN_PIDS (976) to preserve; NO_PIN_PIDS (121) net to 0 ---------
    print("\n=== 4. Pin 976 -> preserve 0; 121 paired-delete nets to 0 (no pin) ===")
    after = snapshot_stock(conn, affected_pids)
    pins = 0
    for pid in affected_pids:
        if pid in PIN_PIDS:
            delta = stock_before[pid] - after[pid]
            if abs(delta) > 1e-9:
                conn.execute(
                    "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
                    "reference_no, note, created_at) VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)",
                    (pid, delta, PIN_NOTE, date.today().isoformat() + ' 00:00:00'))
                pins += 1
                print(f"    pid {pid}: PIN {delta:+g} -> preserve {stock_before[pid]}")
            else:
                print(f"    pid {pid}: PIN pid, delta 0")
        else:
            print(f"    pid {pid}: no pin (paired nets 0) {stock_before[pid]} -> {after[pid]}")

    # STEP 5: verify ------------------------------------------------------------
    print("\n=== 5. VERIFY ===")
    final = snapshot_stock(conn, affected_pids)
    for pid in affected_pids:
        check(f"pid {pid}: final == EXPECTED_FINAL", abs(final[pid] - EXPECTED_FINAL[pid]) < 1e-9,
              f"{final[pid]} == {EXPECTED_FINAL[pid]}")
    check("no negative", all(final[p] >= 0 for p in affected_pids),
          str({p: final[p] for p in affected_pids if final[p] < 0}))
    st_after = one(conn, "SELECT COUNT(*) c FROM sales_transactions")['c']
    check("sales_transactions UNCHANGED", st_after == st_before, f"{st_after} == {st_before}")
    txn_after = one(conn, "SELECT COUNT(*) c FROM transactions")['c']
    check("transactions count == before - deleted + pins",
          txn_after == txn_before - len(present) + pins,
          f"{txn_after} == {txn_before - len(present) + pins}")
    gone = [t for t in present if one(conn, "SELECT 1 FROM transactions WHERE id=?", (t,))]
    check("every deleted id gone", not gone, str(gone))

    _finalize(results, len(present), pins)


def _finalize(results, deleted=0, pins=0):
    failed = [n for n, ok in results if not ok]
    print(f"\n=== SUMMARY: {len(results)-len(failed)}/{len(results)} PASSED | deleted={deleted} | pins={pins} ===")
    if failed:
        raise AssertionError(f"{len(failed)} failed: {failed} — rolling back.")


if __name__ == '__main__':
    main()
