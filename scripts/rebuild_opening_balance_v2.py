"""Rebuild the opening balance v2 — three buckets, all driven from the
opening ADJUST @2024-01-03 (3/1/2567) so stock at the BSN cutoff 3/3/2569
(2026-03-03) hits the right target. Post-cutoff real movements carry forward.

Targets (Σ quantity_change where created_at ≤ 2026-03-03 must equal `target`):

  bucket A  product IN Opening_Stock CSV          target = CSV Pieces
  bucket 1  product NOT in CSV (default)          target = 0
  bucket 3  product NOT in CSV but physically
            counted on 2026-04-07 (sane set)      target = that 4/7 count
            → the stale 2026-04-07 นับสต็อก row(s) are archived + removed
              (its value is relocated into the @2024-01-03 opening instead).

  DEFER set product is ratio-broken (unit_conversions ratio inflates BSN
            OUT 6-digit). A backup back-solve would bake the inflation in,
            so these are left COMPLETELY UNTOUCHED here (no opening removed,
            no opening inserted, 4/7 count kept) and handled by the separate
            unit-ratio fix task. Listed in rebuild_v2_deferred.txt.

Why bucket 3 ≠ backup: every backup (back to 2026-05-07) already holds the
ratio-inflated 6-digit garbage for the DOME/CSK 4-4 products, so no backup
is a clean source for them. The 2026-04-07 physical count IS the clean
source for the sane non-CSV counted items.

Removed opening/loss notes (archived first): ยอดต้นปี% ,
opening adjust auto-corrected% , ยอดสูญหาย%  — EXCEPT for DEFER pids.
KEEPS: opening-balance split% , นับสต็อก (except bucket-3 4/7 rows),
แก้ไขข้อผิดพลาด, ปรับสต็อค, all BSN, history pairs. Does NOT touch
product_code_mapping / unit_conversions / products.sku_code / product_name.

Dry-run by default. --apply commits. Back up first (UNIQUE filename — the
daily backup_db.sh reuses the same-day name).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
_INPUT_DIR = Path(os.environ.get("SENDY_INPUT_DIR", os.path.expanduser("~/Downloads")))
DEFAULT_CSV = _INPUT_DIR / "Inventory Management - Opening_Stock (1).csv"

sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

CUTOFF = "2026-03-03 23:59:59"          # 3/3/2569 BSN cutoff
OPENING_TS = "2024-01-03 00:00:00"      # 3/1/2567 — existing convention
OPENING_NOTE = "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)"
REMOVE_LIKE = ("ยอดต้นปี%", "opening adjust auto-corrected%", "ยอดสูญหาย%")
COUNT_DATE = "2026-04-07"               # the physical-count day
COUNT_LIKE = "นับ%"                      # นับสต็อก / นับสต็อค / นับสต็อก (...)

# ratio-broken — handled by the separate unit-ratio fix task, NOT here.
# v1 9-flagged extreme-opening + ตะปูยิงรีเวท (huge negative from ratio).
DEFER_PIDS = {400, 401, 402, 456, 457, 458, 459, 461, 547, 882, 883}
# non-CSV products physically counted 2026-04-07 with a sane count (not
# ratio-broken). target = the 2026-04-07 count; its 4/7 row is relocated.
BUCKET3_PIDS = {866, 867, 906, 977, 980, 981}


def parse_int(s):
    s = str(s).replace(",", "").strip()
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def load_csv(path):
    """sku(int) -> target pieces. Skip non-int sku and blank Pieces."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            raw = (r.get("SKU (Order)") or "").strip()
            try:
                sku = int(raw)
            except ValueError:
                continue
            pieces_raw = (r.get("Pieces") or "").strip()
            if pieces_raw == "":
                continue
            out[sku] = parse_int(pieces_raw)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true",
                   help="commit (default dry-run)")
    args = p.parse_args(argv)
    if not args.csv_path.exists():
        print(f"CSV not found: {args.csv_path}", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    import models  # noqa: E402

    csv_sku = load_csv(args.csv_path)
    sku2pid = {r["sku"]: r["id"] for r in conn.execute(
        "SELECT id, sku FROM products")}
    all_pids = [r["id"] for r in conn.execute("SELECT id FROM products")]
    csv_pid = {}
    unmatched = []
    for sku, pieces in csv_sku.items():
        pid = sku2pid.get(sku)
        (csv_pid.__setitem__(pid, pieces) if pid else unmatched.append(sku))

    def count_47(pid):
        return conn.execute(
            f"SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            f"WHERE product_id=? AND txn_type='ADJUST' "
            f"AND date(created_at)=? AND note LIKE ?",
            (pid, COUNT_DATE, COUNT_LIKE)).fetchone()[0]

    # classify every product into a bucket; build pid -> (bucket, target)
    targets = {}
    for pid in all_pids:
        if pid in DEFER_PIDS:
            continue                                   # untouched
        if pid in csv_pid:
            targets[pid] = ("A", csv_pid[pid])         # CSV Pieces
        elif pid in BUCKET3_PIDS:
            targets[pid] = ("3", count_47(pid))        # 4/7 physical count
        else:
            targets[pid] = ("1", 0)                    # non-CSV → 0

    where_remove = " OR ".join("note LIKE ?" for _ in REMOVE_LIKE)
    defer_csv = ",".join(str(x) for x in DEFER_PIDS) or "NULL"

    # rows to archive+remove: opening/loss notes (NOT for defer pids) ...
    removed = conn.execute(
        f"SELECT id,product_id,txn_type,quantity_change,note,created_at "
        f"FROM transactions WHERE txn_type='ADJUST' AND ({where_remove}) "
        f"AND product_id NOT IN ({defer_csv})", REMOVE_LIKE).fetchall()
    # ... plus the stale 2026-04-07 นับสต็อก rows for bucket-3 pids only.
    b3_csv = ",".join(str(x) for x in sorted(BUCKET3_PIDS)) or "NULL"
    removed_b3 = conn.execute(
        f"SELECT id,product_id,txn_type,quantity_change,note,created_at "
        f"FROM transactions WHERE txn_type='ADJUST' AND date(created_at)=? "
        f"AND note LIKE ? AND product_id IN ({b3_csv})",
        (COUNT_DATE, COUNT_LIKE)).fetchall()
    all_removed = list(removed) + list(removed_b3)

    arch = EXPORTS / f"removed_opening_adjusts_v2_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "product_id", "txn_type", "quantity_change",
                    "note", "created_at"])
        for r in all_removed:
            w.writerow([r["id"], r["product_id"], r["txn_type"],
                        r["quantity_change"], r["note"], r["created_at"]])
    removed_pids = {r["product_id"] for r in all_removed}
    removed_ids = [r["id"] for r in all_removed]

    # remaining-to-cutoff EXCLUDING the to-be-removed rows (works pre/post
    # delete identically): opening = target − remaining.
    rid_set = set(removed_ids)

    def remaining(pid):
        rows = conn.execute(
            "SELECT id,quantity_change FROM transactions "
            "WHERE product_id=? AND created_at<=?", (pid, CUTOFF)).fetchall()
        return sum(r["quantity_change"] for r in rows if r["id"] not in rid_set)

    plan, nonzero = [], 0
    bucket_n = {"A": 0, "1": 0, "3": 0}
    for pid, (b, tgt) in sorted(targets.items()):
        rem = remaining(pid)
        op = tgt - rem
        bucket_n[b] += 1
        if op != 0:
            nonzero += 1
        plan.append((pid, b, tgt, rem, op))

    print(f"=== rebuild v2 | products {len(all_pids)} "
          f"| CSV sku {len(csv_sku)} unmatched {len(unmatched)} ===")
    print(f"  bucket A (CSV→Pieces)        : {bucket_n['A']}")
    print(f"  bucket 1 (non-CSV→0)         : {bucket_n['1']}")
    print(f"  bucket 3 (non-CSV→4/7 count) : {bucket_n['3']}  "
          f"{sorted(BUCKET3_PIDS)}")
    print(f"  DEFER (ratio-broken, kept)   : {len(DEFER_PIDS)}  "
          f"{sorted(DEFER_PIDS)}")
    print(f"  opening/loss ADJUSTs removed : {len(removed)} "
          f"(+{len(removed_b3)} bucket-3 4/7 นับ)  → {arch.name}")
    print(f"  opening rows to insert       : {nonzero}")

    with open(EXPORTS / "rebuild_v2_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "bucket", "target",
                    "remaining_to_cutoff", "opening_set"])
        w.writerows(plan)
    with open(EXPORTS / "rebuild_v2_unmatched.csv", "w", newline="") as f:
        csv.writer(f).writerows([["sku_not_in_products"]] +
                                [[s] for s in sorted(unmatched)])
    (EXPORTS / "rebuild_v2_deferred.txt").write_text(
        "DEFER (ratio-broken — handled by unit-ratio fix task, untouched):\n" +
        "\n".join(str(x) for x in sorted(DEFER_PIDS)))

    if not args.apply:
        print("\nDRY-RUN. Back up with a UNIQUE filename, then --apply.")
        print(f"Review: {EXPORTS}/rebuild_v2_summary.csv, "
              f"rebuild_v2_unmatched.csv, rebuild_v2_deferred.txt, {arch.name}")
        conn.close()
        return 0

    # ---- APPLY ----
    if removed_ids:
        conn.executemany("DELETE FROM transactions WHERE id=?",
                          [(i,) for i in removed_ids])
    inserted = 0
    for pid, (b, tgt) in sorted(targets.items()):
        rem = conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            "WHERE product_id=? AND created_at<=?", (pid, CUTOFF)).fetchone()[0]
        op = tgt - rem
        if op != 0:
            conn.execute(
                "INSERT INTO transactions (product_id,txn_type,"
                "quantity_change,unit_mode,reference_no,note,created_at) "
                "VALUES (?,'ADJUST',?,'unit',NULL,?,?)",
                (pid, op, OPENING_NOTE, OPENING_TS))
            inserted += 1
    affected = sorted(removed_pids | set(targets))
    for pid in affected:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
        conn.execute(
            "INSERT INTO stock_levels (product_id,quantity) "
            "SELECT product_id, COALESCE(SUM(quantity_change),0) "
            "FROM transactions WHERE product_id=?", (pid,))
    conn.commit()
    for pid in affected:
        try:
            models.recalculate_product_wacc(pid, conn)
        except Exception as e:
            print(f"  [warn] WACC pid {pid}: {e}")
    conn.commit()

    # verify: Σ(qty ≤ cutoff) == target for every targeted (non-defer) pid
    bad = []
    for pid, (b, tgt) in targets.items():
        s = conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            "WHERE product_id=? AND created_at<=?", (pid, CUTOFF)).fetchone()[0]
        if s != tgt:
            bad.append((pid, b, tgt, s))
    neg = conn.execute(
        "SELECT COUNT(*) FROM stock_levels WHERE quantity<0").fetchone()[0]
    neg_defer = conn.execute(
        f"SELECT COUNT(*) FROM stock_levels WHERE quantity<0 "
        f"AND product_id IN ({defer_csv})").fetchone()[0]
    (EXPORTS / "rebuild_v2_negatives.txt").write_text("\n".join(
        f"pid {r['product_id']} qty {r['quantity']}" for r in conn.execute(
            "SELECT product_id,quantity FROM stock_levels "
            "WHERE quantity<0 ORDER BY quantity")))
    print(f"\nAPPLIED. removed {len(all_removed)} rows, inserted {inserted} "
          f"opening rows, recalced {len(affected)} products.")
    print(f"  cutoff==target mismatches: {len(bad)} "
          f"{'OK' if not bad else bad[:5]}")
    print(f"  products negative now: {neg} "
          f"(of which {neg_defer} are DEFER ratio-broken — expected)")
    conn.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
