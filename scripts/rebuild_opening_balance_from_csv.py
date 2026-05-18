"""Rebuild the opening balance so stock at the BSN cutoff 3/3/2569
(2026-03-03) equals the physical count in the Opening_Stock CSV.

Why: the old opening balances were back-solved from `current − BSN history`
assuming BSN history was complete. After the BSN re-import + mapping/unit
re-sync, cumulative BSN OUT now exceeds opening+IN for some products → false
negatives. Fix: discard the script-made opening/loss ADJUSTs and re-derive a
single opening ADJUST @2024-01-03 per CSV product so that
  Σ(quantity_change where created_at ≤ 2026-03-03) == CSV Pieces.
Post-cutoff real movements carry forward from there.

Scope / safety:
  - Removes ONLY notes: ยอดต้นปี% , opening adjust auto-corrected% ,
    ยอดสูญหาย% . KEEPS: opening-balance split% (ตัว/แผง), นับสต็อก,
    แก้ไขข้อผิดพลาด, ปรับสต็อค, all BSN, history pairs.
  - Does NOT touch product_code_mapping / unit_conversions /
    products.sku_code / products.product_name (Put's own edits).
  - Products not in the CSV → opening 0 (no row); their stock = movements only.
  - Archives every removed row to a CSV first (audit).

Dry-run by default. --apply commits. Back up first (scripts/backup_db.sh).
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
DEFAULT_CSV = Path(
    "/Users/putty/Downloads/Inventory Management - Opening_Stock (1).csv")

sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

CUTOFF = "2026-03-03 23:59:59"          # 3/3/2569 BSN cutoff
OPENING_TS = "2024-01-03 00:00:00"      # existing convention (report excludes this date)
OPENING_NOTE = "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)"
# notes removed (archived first). NOTE: must NOT match 'opening-balance split%'.
REMOVE_LIKE = ("ยอดต้นปี%", "opening adjust auto-corrected%", "ยอดสูญหาย%")


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
                continue                       # blank → not counted
            out[sku] = parse_int(pieces_raw)   # explicit 0 kept as target 0
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true", help="commit (default dry-run)")
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
    matched, unmatched = {}, []
    for sku, pieces in csv_sku.items():
        pid = sku2pid.get(sku)
        (matched.__setitem__(pid, pieces) if pid else unmatched.append(sku))

    where_remove = " OR ".join("note LIKE ?" for _ in REMOVE_LIKE)
    removed = conn.execute(
        f"SELECT id,product_id,txn_type,quantity_change,note,created_at "
        f"FROM transactions WHERE txn_type='ADJUST' AND ({where_remove})",
        REMOVE_LIKE,
    ).fetchall()
    arch = EXPORTS / f"removed_opening_adjusts_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "product_id", "txn_type", "quantity_change",
                    "note", "created_at"])
        for r in removed:
            w.writerow([r["id"], r["product_id"], r["txn_type"],
                        r["quantity_change"], r["note"], r["created_at"]])
    removed_pids = {r["product_id"] for r in removed}

    # remaining-to-cutoff EXCLUDING the to-be-removed notes (works pre/post
    # delete identically); opening = target − remaining.
    def remaining_to_cutoff(pid):
        return conn.execute(
            f"SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            f"WHERE product_id=? AND created_at<=? "
            f"AND NOT (txn_type='ADJUST' AND ({where_remove}))",
            (pid, CUTOFF, *REMOVE_LIKE),
        ).fetchone()[0]

    plan_rows, opening_nonzero = [], 0
    for pid, pieces in sorted(matched.items()):
        rem = remaining_to_cutoff(pid)
        opening = pieces - rem
        if opening != 0:
            opening_nonzero += 1
        plan_rows.append((pid, pieces, rem, opening))

    print(f"=== Opening_Stock CSV: {len(csv_sku)} sku | matched {len(matched)} "
          f"| unmatched {len(unmatched)} ===")
    print(f"  ADJUSTs to remove (archived): {len(removed)} "
          f"(pids {len(removed_pids)})  → {arch.name}")
    print(f"  opening rows to insert (nonzero): {opening_nonzero}")
    big = sorted(plan_rows, key=lambda x: -abs(x[3]))[:8]
    print("  largest |opening|:")
    for pid, pc, rem, op in big:
        print(f"    pid {pid}  csv={pc}  remaining={rem}  opening={op}")

    with open(EXPORTS / "rebuild_opening_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "csv_pieces", "remaining_to_cutoff",
                    "opening_set"])
        w.writerows(plan_rows)
    with open(EXPORTS / "rebuild_csv_unmatched.csv", "w", newline="") as f:
        csv.writer(f).writerows([["sku_not_in_products"]] +
                                [[s] for s in sorted(unmatched)])

    if not args.apply:
        print("\nDRY-RUN. Back up (scripts/backup_db.sh) then re-run --apply.")
        print(f"Review: {EXPORTS}/rebuild_opening_summary.csv, "
              f"rebuild_csv_unmatched.csv, {arch.name}")
        conn.close()
        return 0

    # ---- APPLY ----
    conn.execute(
        f"DELETE FROM transactions WHERE txn_type='ADJUST' AND ({where_remove})",
        REMOVE_LIKE)
    inserted = 0
    for pid, pieces in sorted(matched.items()):
        rem = conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            "WHERE product_id=? AND created_at<=?", (pid, CUTOFF)).fetchone()[0]
        opening = pieces - rem
        if opening != 0:
            conn.execute(
                "INSERT INTO transactions (product_id,txn_type,quantity_change,"
                "unit_mode,reference_no,note,created_at) "
                "VALUES (?,'ADJUST',?,'unit',NULL,?,?)",
                (pid, opening, OPENING_NOTE, OPENING_TS))
            inserted += 1
    affected = sorted(removed_pids | set(matched))
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

    # in-script verification: Σ(qty ≤ cutoff) == CSV pieces for every matched pid
    bad = []
    for pid, pieces in matched.items():
        s = conn.execute(
            "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
            "WHERE product_id=? AND created_at<=?", (pid, CUTOFF)).fetchone()[0]
        if s != pieces:
            bad.append((pid, pieces, s))
    neg_noncsv = conn.execute(
        "SELECT product_id,quantity FROM stock_levels WHERE quantity<0 "
        "AND product_id NOT IN (%s) ORDER BY quantity"
        % (",".join(str(p) for p in matched) or "NULL")).fetchall()
    (EXPORTS / "rebuild_noncsv_negatives.txt").write_text(
        "\n".join(f"pid {r['product_id']} qty {r['quantity']}"
                  for r in neg_noncsv))
    print(f"\nAPPLIED. removed {len(removed)} ADJUSTs, inserted {inserted} "
          f"opening rows, recalced {len(affected)} products.")
    print(f"  cutoff==CSV mismatches: {len(bad)} "
          f"{'OK' if not bad else bad[:5]}")
    print(f"  non-CSV products still negative: {len(neg_noncsv)} "
          f"(rebuild_noncsv_negatives.txt — NOT auto-fixed)")
    conn.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
