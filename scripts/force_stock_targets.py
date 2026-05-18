"""Force current stock to an explicit target, by rebuilding the single
opening ADJUST @2024-01-03 (3/1/2567) — per Put's 2026-05-18 decision.

Two target groups:
  DEFER (ratio-broken, 11 pids)  target = that product's stock in the
        reference backup DB. Put confirmed 6-digit ลูกรีเวท counts are
        real (unit = ดอก, tiny pieces). We honour the old DB value.
  NEG   every other product currently negative  target = 0 (Put: fix the
        stock — "current = 0 instead of negative", original Point 1).

Per pid: archive then remove its opening/loss ADJUSTs
(ยอดต้นปี% / opening adjust auto-corrected% / ยอดสูญหาย%) AND its stale
2026-04-07 นับสต็อก row(s) (delta computed against the old inflated stock —
garbage); set opening = target − Σ(remaining qty); insert ONE ADJUST
@2024-01-03; recalc stock_levels + WACC; verify current == target.

Does NOT touch product_code_mapping / unit_conversions / sku_code /
product_name / opening-balance split% / BSN rows / history pairs.

Dry-run by default. --apply commits. Back up with a UNIQUE filename first.
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
DEFAULT_BACKUP = (ROOT / "inventory_app" / "instance" /
                  "inventory_backup_before_mapping_update_20260515_151522.db")

sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

OPENING_TS = "2024-01-03 00:00:00"      # 3/1/2567
OPENING_NOTE = ("ยอดต้นปี (force target ตามที่ Put สั่ง 2026-05-18 — "
                "DEFER=backup 2026-05-15 / neg→0)")
REMOVE_LIKE = ("ยอดต้นปี%", "opening adjust auto-corrected%", "ยอดสูญหาย%")
COUNT_DATE = "2026-04-07"
COUNT_LIKE = "นับ%"
DEFER_PIDS = [400, 401, 402, 456, 457, 458, 459, 461, 547, 882, 883]


def build_targets(conn, backup_path):
    """pid -> (group, target). DEFER from backup; all other negatives -> 0."""
    t = {}
    b = sqlite3.connect(str(backup_path))
    for pid in DEFER_PIDS:
        r = b.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                       (pid,)).fetchone()
        t[pid] = ("DEFER", r[0] if r else 0)
    b.close()
    for r in conn.execute(
            "SELECT product_id FROM stock_levels WHERE quantity<0"):
        pid = r[0]
        if pid not in t:
            t[pid] = ("NEG", 0)
    return t


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--backup", type=Path, default=DEFAULT_BACKUP,
                   help="reference DB for DEFER targets")
    p.add_argument("--apply", action="store_true",
                   help="commit (default dry-run)")
    args = p.parse_args(argv)
    if not args.backup.exists():
        print(f"backup not found: {args.backup}", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    import models  # noqa: E402

    targets = build_targets(conn, args.backup)
    pid_csv = ",".join(str(x) for x in targets) or "NULL"

    where_remove = " OR ".join("note LIKE ?" for _ in REMOVE_LIKE)
    rm_open = conn.execute(
        f"SELECT id,product_id,txn_type,quantity_change,note,created_at "
        f"FROM transactions WHERE txn_type='ADJUST' AND ({where_remove}) "
        f"AND product_id IN ({pid_csv})", REMOVE_LIKE).fetchall()
    rm_count = conn.execute(
        f"SELECT id,product_id,txn_type,quantity_change,note,created_at "
        f"FROM transactions WHERE txn_type='ADJUST' AND date(created_at)=? "
        f"AND note LIKE ? AND product_id IN ({pid_csv})",
        (COUNT_DATE, COUNT_LIKE)).fetchall()
    removed = list(rm_open) + list(rm_count)
    rm_ids = {r["id"] for r in removed}

    arch = EXPORTS / f"removed_force_targets_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "product_id", "txn_type", "quantity_change",
                    "note", "created_at"])
        for r in removed:
            w.writerow([r["id"], r["product_id"], r["txn_type"],
                        r["quantity_change"], r["note"], r["created_at"]])

    def remaining(pid):
        rows = conn.execute(
            "SELECT id,quantity_change FROM transactions WHERE product_id=?",
            (pid,)).fetchall()
        return sum(r["quantity_change"] for r in rows if r["id"] not in rm_ids)

    plan = []
    for pid, (grp, tgt) in sorted(targets.items()):
        rem = remaining(pid)
        plan.append((pid, grp, tgt, rem, tgt - rem))
    n_defer = sum(1 for v in targets.values() if v[0] == "DEFER")
    n_neg = len(targets) - n_defer
    print(f"=== force_stock_targets | DEFER {n_defer} (→backup) "
          f"| NEG {n_neg} (→0) ===")
    print(f"  removing {len(rm_open)} opening/loss + {len(rm_count)} "
          f"stale นับสต็อก@{COUNT_DATE}  → {arch.name}")
    print("  DEFER targets:")
    for pid, grp, tgt, rem, op in plan:
        if grp == "DEFER":
            print(f"    pid {pid:5} target={tgt:>10} (remaining {rem} "
                  f"→ opening {op})")
    with open(EXPORTS / "force_targets_plan.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "group", "target", "remaining",
                    "opening_set"])
        w.writerows(plan)

    if not args.apply:
        print(f"\nDRY-RUN. Unique backup then --apply. Plan: "
              f"{EXPORTS}/force_targets_plan.csv, {arch.name}")
        conn.close()
        return 0

    # ---- APPLY ----
    if rm_ids:
        conn.executemany("DELETE FROM transactions WHERE id=?",
                          [(i,) for i in rm_ids])
    inserted = 0
    for pid, (grp, tgt) in sorted(targets.items()):
        rem = conn.execute("SELECT COALESCE(SUM(quantity_change),0) "
                            "FROM transactions WHERE product_id=?",
                            (pid,)).fetchone()[0]
        op = tgt - rem
        if op != 0:
            conn.execute(
                "INSERT INTO transactions (product_id,txn_type,"
                "quantity_change,unit_mode,reference_no,note,created_at) "
                "VALUES (?,'ADJUST',?,'unit',NULL,?,?)",
                (pid, op, OPENING_NOTE, OPENING_TS))
            inserted += 1
    for pid in targets:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
        conn.execute(
            "INSERT INTO stock_levels (product_id,quantity) "
            "SELECT product_id, COALESCE(SUM(quantity_change),0) "
            "FROM transactions WHERE product_id=?", (pid,))
    conn.commit()
    for pid in targets:
        try:
            models.recalculate_product_wacc(pid, conn)
        except Exception as e:
            print(f"  [warn] WACC pid {pid}: {e}")
    conn.commit()

    bad = []
    for pid, (grp, tgt) in targets.items():
        cur = conn.execute("SELECT quantity FROM stock_levels "
                            "WHERE product_id=?", (pid,)).fetchone()[0]
        if cur != tgt:
            bad.append((pid, grp, tgt, cur))
    neg = conn.execute(
        "SELECT COUNT(*) FROM stock_levels WHERE quantity<0").fetchone()[0]
    print(f"\nAPPLIED. removed {len(removed)}, inserted {inserted}, "
          f"recalced {len(targets)} products.")
    print(f"  current==target mismatches: {len(bad)} "
          f"{'OK' if not bad else bad[:5]}")
    print(f"  products negative now: {neg} "
          f"(expected: DEFER pids whose backup value is negative)")
    conn.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
