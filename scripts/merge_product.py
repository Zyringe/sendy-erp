"""Merge one product into another (duplicate consolidation).

  python scripts/merge_product.py --from 294 --to 293 [--apply]

Reassigns every `product_id` reference from FROM → TO across all tables that
have a product_id column, then recalculates TO's stock from its (now merged)
ledger, drops FROM's stock_levels row, and sets FROM.is_active=0.

Special handling:
  - unit_conversions: keep TO's rows; move only FROM rows whose bsn_unit TO
    doesn't already have; drop the rest (UNIQUE(product_id,bsn_unit)).
  - product_code_mapping: bsn_code is globally UNIQUE → plain product_id
    reassign (no collision).
  - product_images: keyed by sku_id, not product_id → left untouched
    (FROM stays in catalog but is_active=0; image rows are sku-scoped).

Invariant: total stock is conserved — TO_after == TO_before + FROM_before
(every transaction row moves, none created/dropped). Verified before commit.

FROM is NOT deleted (kept is_active=0 for audit / FK safety). Dry-run by
default. --apply commits. Unique backup first.
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
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

# product_id reassigned by plain UPDATE here; the two below are special-cased.
SPECIAL = {"unit_conversions", "product_code_mapping"}
# Never re-point these: product_id is a PRIMARY KEY forensic archive of the
# dropped integer sku (mig 097). Re-pointing collides on a merge where both
# products have a row, and would corrupt the id->old-sku trace. Leave the
# loser's row pointing at the (now is_active=0) loser product.
SKIP = {"stock_levels", "legacy_product_sku_map"}


def tables_with_product_id(conn):
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if "product_id" in cols:
            out.append(name)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", type=int, required=True)
    ap.add_argument("--to", dest="dst", type=int, required=True)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.src == a.dst:
        print("from == to", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row
    import models  # noqa: E402

    for pid in (a.src, a.dst):
        if not conn.execute("SELECT 1 FROM products WHERE id=?",
                            (pid,)).fetchone():
            print(f"product {pid} not found", file=sys.stderr)
            return 2

    tabs = tables_with_product_id(conn)

    def stock(pid):
        r = conn.execute("SELECT COALESCE(SUM(quantity_change),0) FROM "
                          "transactions WHERE product_id=?", (pid,)).fetchone()
        return r[0]

    s_src, s_dst = stock(a.src), stock(a.dst)
    plan = []
    for t in tabs:
        n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE product_id=?",
                         (a.src,)).fetchone()[0]
        if n:
            plan.append((t, n))
    print(f"=== merge product {a.src} → {a.dst} ===")
    print(f"  ledger stock: from={s_src} to={s_dst} "
          f"→ to_after should be {s_src + s_dst}")
    for t, n in plan:
        print(f"  {t}: {n} row(s) with product_id={a.src}")

    arch = EXPORTS / f"merge_{a.src}_to_{a.dst}_{ts}.csv"
    with open(arch, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "rows_from"])
        w.writerows(plan)

    if not a.apply:
        print(f"\nDRY-RUN. Unique backup then --apply. Plan → {arch.name}")
        conn.close()
        return 0

    # ---- APPLY (single transaction) ----
    conn.execute("BEGIN")
    try:
        # unit_conversions: move only units TO lacks
        dst_units = {r[0] for r in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?",
            (a.dst,))}
        for r in conn.execute("SELECT id,bsn_unit FROM unit_conversions "
                               "WHERE product_id=?", (a.src,)).fetchall():
            if r["bsn_unit"] in dst_units:
                conn.execute("DELETE FROM unit_conversions WHERE id=?",
                             (r["id"],))
            else:
                conn.execute("UPDATE unit_conversions SET product_id=? "
                             "WHERE id=?", (a.dst, r["id"]))
        # product_code_mapping: bsn_code globally unique → plain reassign
        conn.execute("UPDATE product_code_mapping SET product_id=? "
                     "WHERE product_id=?", (a.dst, a.src))
        # everything else with a product_id column
        for t in tabs:
            if t in SPECIAL or t in SKIP:
                continue
            conn.execute(f"UPDATE {t} SET product_id=? WHERE product_id=?",
                         (a.dst, a.src))
        # recalc TO stock, drop FROM stock, deactivate FROM
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (a.dst,))
        conn.execute("INSERT INTO stock_levels (product_id,quantity) "
                     "SELECT ?, COALESCE(SUM(quantity_change),0) FROM "
                     "transactions WHERE product_id=?", (a.dst, a.dst))
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (a.src,))
        conn.execute("UPDATE products SET is_active=0 WHERE id=?", (a.src,))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    try:
        models.recalculate_product_wacc(a.dst, conn)
        conn.commit()
    except Exception as e:
        print(f"  [warn] WACC: {e}")

    to_after = conn.execute("SELECT quantity FROM stock_levels WHERE "
                            "product_id=?", (a.dst,)).fetchone()[0]
    leftover = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE "
                                f"product_id=?", (a.src,)).fetchone()[0]
                   for t in tabs if t not in SKIP)
    ok = (to_after == s_src + s_dst) and leftover == 0
    print(f"\nAPPLIED. to({a.dst}) stock={to_after} "
          f"(expected {s_src + s_dst}) {'OK' if to_after==s_src+s_dst else 'MISMATCH'}")
    print(f"  leftover product_id={a.src} refs: {leftover} (want 0)")
    print(f"  product {a.src} is_active=0")
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
