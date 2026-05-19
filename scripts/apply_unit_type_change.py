"""Bucket D1 — change a product's base unit_type and convert its ledger.

Reads the reviewed CSV. For decision text:
  "change the unit from <X> to <Y> then convert old stock by x<N>.
   Leave the bsn's ratio suggestion at <M>"
do, per product:
  - UPDATE products.unit_type = Y
  - multiply EVERY transactions.quantity_change of that product by N
    (so e.g. 1 โหล → 12 อัน); recalc stock_levels (= old × N)
  - set unit_conversions(product, <that row's bsn_unit>).ratio = M
    (each CSV row of the product carries its own bsn_unit + M)

Conserves the convert-by-N invariant: new_stock == old_stock * N.
Does NOT touch product_code_mapping or other products. Dry-run by
default. --apply commits. Unique backup first.

D2 ("1 and change ตัว unit to Y" — N not given) is NOT handled here;
--dump-d2 writes a fill-in CSV for Put.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
DEFAULT_CSV = Path("/Users/putty/Downloads/stock_mapping_suggested_20260518"
                   " - stock_mapping_suggested_20260518.csv")
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

DEC = "decision"
D1_RE = re.compile(r"change the unit from (\S+) to (\S+).*?convert old "
                   r"stock by x(\d+).*?ratio suggestion at (\d+)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dump-d2", action="store_true",
                    help="write bucket_D2_fill_N.csv and exit")
    ap.add_argument("--d2", type=Path,
                    help="apply the filled bucket_D2_fill_N.csv")
    a = ap.parse_args(argv)
    if not a.csv_path.exists():
        print(f"CSV not found: {a.csv_path}", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row

    if a.d2:
        return _run_d2(conn, a)

    # group D1 by product
    prod = {}                         # pid -> {to,N,from}
    ratios = defaultdict(dict)        # pid -> {bsn_unit: M}
    d2 = []
    with open(a.csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get(DEC) or "").strip()
            try:
                float(d)
                continue
            except ValueError:
                pass
            if not d or d == "-":
                continue
            pid = int(r["product_id"])
            m = D1_RE.search(d)
            if m:
                prod[pid] = {"from": m.group(1), "to": m.group(2),
                             "N": int(m.group(3))}
                bu = (r.get("bsn_unit") or "").strip()
                if bu:
                    ratios[pid][bu] = int(m.group(4))
            elif d.startswith("1 and change"):
                mm = re.search(r"change (\S+) unit to (\S+)", d)
                d2.append((pid, r.get("product_name", ""),
                           mm.group(1) if mm else "?",
                           mm.group(2) if mm else "?",
                           (r.get("bsn_unit") or "").strip()))

    if a.dump_d2:
        seen, rows = set(), []
        for pid, nm, fr, to, bu in d2:
            if pid in seen:
                continue
            seen.add(pid)
            st = conn.execute("SELECT quantity FROM stock_levels WHERE "
                              "product_id=?", (pid,)).fetchone()
            rows.append([pid, nm, fr, to, st[0] if st else 0, ""])
        out = EXPORTS / "bucket_D2_fill_N.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["product_id", "product_name", "from_unit",
                        "to_unit", "current_stock", "convert_by_N"])
            w.writerows(rows)
        print(f"{out}  ({len(rows)} products) — Put: fill convert_by_N")
        conn.close()
        return 0

    print(f"=== Bucket D1 unit_type change | {len(prod)} products ===")
    plan = []
    for pid, info in sorted(prod.items()):
        p = conn.execute("SELECT product_name,unit_type FROM products "
                         "WHERE id=?", (pid,)).fetchone()
        st = conn.execute("SELECT COALESCE(SUM(quantity_change),0) FROM "
                          "transactions WHERE product_id=?",
                          (pid,)).fetchone()[0]
        plan.append((pid, info, st))
        print(f"  pid {pid} '{p['product_name'][:30]:30}' "
              f"{p['unit_type']}→{info['to']} ×{info['N']} "
              f"stock {st}→{st * info['N']}  ratios={dict(ratios[pid])}")

    if not a.apply:
        print("\nDRY-RUN. Unique backup then --apply.  "
              "(D2 fill-in: --dump-d2)")
        conn.close()
        return 0

    conn.execute("BEGIN")
    try:
        for pid, info, _ in plan:
            conn.execute("UPDATE products SET unit_type=? WHERE id=?",
                         (info["to"], pid))
            conn.execute("UPDATE transactions SET quantity_change="
                         "quantity_change*? WHERE product_id=?",
                         (info["N"], pid))
            conn.execute("DELETE FROM stock_levels WHERE product_id=?",
                         (pid,))
            conn.execute("INSERT INTO stock_levels (product_id,quantity) "
                         "SELECT ?,COALESCE(SUM(quantity_change),0) FROM "
                         "transactions WHERE product_id=?", (pid, pid))
            for bu, M in ratios[pid].items():
                if conn.execute("SELECT 1 FROM unit_conversions WHERE "
                                "product_id=? AND bsn_unit=?",
                                (pid, bu)).fetchone():
                    conn.execute("UPDATE unit_conversions SET ratio=? WHERE "
                                 "product_id=? AND bsn_unit=?",
                                 (M, pid, bu))
                else:
                    conn.execute("INSERT INTO unit_conversions (product_id,"
                                 "bsn_unit,ratio) VALUES (?,?,?)",
                                 (pid, bu, M))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    bad = []
    for pid, info, old in plan:
        nu = conn.execute("SELECT unit_type FROM products WHERE id=?",
                          (pid,)).fetchone()[0]
        ns = conn.execute("SELECT quantity FROM stock_levels WHERE "
                          "product_id=?", (pid,)).fetchone()
        ns = ns[0] if ns else 0
        if nu != info["to"] or abs(ns - old * info["N"]) > 1e-6:
            bad.append((pid, nu, ns, old * info["N"]))
    print(f"\nAPPLIED. {len(plan)} products. mismatches: "
          f"{len(bad)} {bad[:5] if bad else 'OK'}")
    conn.close()
    return 0 if not bad else 1


def _run_d2(conn, a):
    """Apply the filled bucket_D2_fill_N.csv: decision was
    '1 and change <X> unit to <Y>' → unit_type=Y, transactions ×N,
    recalc stock, set ratio=1 for that product's D2 bsn_units."""
    if not a.d2.exists():
        print(f"D2 csv not found: {a.d2}", file=sys.stderr)
        return 2
    plan = {}
    with open(a.d2, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                pid = int(r["product_id"])
                n = int(float(r["convert_by_N"]))
            except (TypeError, ValueError):
                continue
            plan[pid] = (r["to_unit"].strip(), n)
    # bsn_units that carried a D2 decision for each pid → ratio 1
    bunits = defaultdict(set)
    with open(a.csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get(DEC) or "").strip()
            if not d.startswith("1 and change"):
                continue
            try:
                pid = int(r["product_id"])
            except (TypeError, ValueError):
                continue
            bu = (r.get("bsn_unit") or "").strip()
            if pid in plan and bu:
                bunits[pid].add(bu)

    print(f"=== Bucket D2 | {len(plan)} products (ratio→1) ===")
    rows = []
    for pid, (to, n) in sorted(plan.items()):
        p = conn.execute("SELECT product_name,unit_type FROM products "
                         "WHERE id=?", (pid,)).fetchone()
        st = conn.execute("SELECT COALESCE(SUM(quantity_change),0) FROM "
                          "transactions WHERE product_id=?",
                          (pid,)).fetchone()[0]
        rows.append((pid, to, n, st))
        print(f"  pid {pid} '{p['product_name'][:30]:30}' "
              f"{p['unit_type']}→{to} ×{n} stock {st}→{st * n} "
              f"ratio1={sorted(bunits[pid])}")
    if not a.apply:
        print("\nDRY-RUN. Unique backup then --apply.")
        conn.close()
        return 0
    conn.execute("BEGIN")
    try:
        for pid, to, n, _ in rows:
            conn.execute("UPDATE products SET unit_type=? WHERE id=?",
                         (to, pid))
            if n != 1:
                conn.execute("UPDATE transactions SET quantity_change="
                             "quantity_change*? WHERE product_id=?",
                             (n, pid))
            conn.execute("DELETE FROM stock_levels WHERE product_id=?",
                         (pid,))
            conn.execute("INSERT INTO stock_levels (product_id,quantity) "
                         "SELECT ?,COALESCE(SUM(quantity_change),0) FROM "
                         "transactions WHERE product_id=?", (pid, pid))
            for bu in bunits[pid]:
                if conn.execute("SELECT 1 FROM unit_conversions WHERE "
                                "product_id=? AND bsn_unit=?",
                                (pid, bu)).fetchone():
                    conn.execute("UPDATE unit_conversions SET ratio=1 WHERE "
                                 "product_id=? AND bsn_unit=?", (pid, bu))
                else:
                    conn.execute("INSERT INTO unit_conversions (product_id,"
                                 "bsn_unit,ratio) VALUES (?,?,1)", (pid, bu))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1
    bad = [pid for pid, to, n, _ in rows if conn.execute(
        "SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()[0]
        != to]
    print(f"\nAPPLIED. {len(rows)} products. unit_type mismatches: "
          f"{len(bad)} {bad or 'OK'}")
    conn.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
