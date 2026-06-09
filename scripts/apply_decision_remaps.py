"""DEPRECATED: one-off from 2026-05-19. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Bucket C+E — re-map bsn_code/bsn_unit to the product Put named in the
review CSV (decision blank or '-', remap-target column filled), with
unit_conversions ratio = 1. Both products stay active (split preserved).

If the target product_name does not exist:
  - clone the sibling row (same name with the packaging suffix swapped,
    e.g. (แผง)↔(ตัว)) — all columns copied, then product_name / packaging /
    sku_code-suffix / id overridden;
  - if no sibling either → minimal new row (name + unit_type guessed from
    the "(...)" suffix) and FLAG it for Put to complete.

Then: product_code_mapping(bsn_code) → target; move all sales/purchase
ledger rows of that code → target; recalc stock for old+target; upsert
unit_conversions(target, bsn_unit, 1).

Dry-run by default (prints every new SKU's derived fields as the
checkpoint). --apply commits. Unique backup first.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
EXPORTS = ROOT / "data" / "exports"
_INPUT_DIR = Path(os.environ.get("SENDY_INPUT_DIR", os.path.expanduser("~/Downloads")))
DEFAULT_CSV = _INPUT_DIR / ("stock_mapping_suggested_20260518"
                            " - stock_mapping_suggested_20260518.csv")
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

DEC = "decision"
REMAP = ("change mapping of particular bsn_name and bsn_unit to following "
         "product (with conversion = 1)")
LEDGER = ("sales_transactions", "purchase_transactions")


def suffix_unit(name):
    m = re.search(r"\(([^()]+)\)\s*$", name or "")
    return m.group(1).strip() if m else None


def stripped(name):
    return re.sub(r"\((?:ตัว|แผง)\)", "", name or "").strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if not a.csv_path.exists():
        print(f"CSV not found: {a.csv_path}", file=sys.stderr)
        return 2
    EXPORTS.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    conn = sqlite3.connect(str(a.db))
    conn.row_factory = sqlite3.Row

    name2id = {r["product_name"]: r["id"] for r in conn.execute(
        "SELECT id,product_name FROM products")}
    pcols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]

    actions = []          # (bsn_code, bsn_unit, target_name, mode, sibling)
    seen = set()
    with open(a.csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = (r.get(DEC) or "").strip()
            rm = (r.get(REMAP) or "").strip()
            code = (r.get("bsn_code") or "").strip()
            unit = (r.get("bsn_unit") or "").strip()
            if not rm or rm == "-" or d not in ("", "-") or not code:
                continue
            key = (code, unit, rm)
            if key in seen:
                continue
            seen.add(key)
            if rm in name2id:
                actions.append((code, unit, rm, "existing", None))
            else:
                sib = [nm for nm in name2id
                       if stripped(nm) == stripped(rm) and nm != rm]
                actions.append((code, unit, rm,
                                "clone" if sib else "minimal",
                                sib[0] if sib else None))

    print(f"=== Bucket C+E remap | {len(actions)} (code,unit) ===")
    nextid = conn.execute("SELECT MAX(id)+1 FROM products").fetchone()[0]
    new_rows = []
    for code, unit, tname, mode, sib in actions:
        if mode == "existing":
            print(f"  {code} {unit} → EXISTING pid {name2id[tname]} "
                  f"'{tname}'")
            continue
        ut = suffix_unit(tname) or "ตัว"
        if mode == "clone":
            srow = conn.execute("SELECT * FROM products WHERE id=?",
                                (name2id[sib],)).fetchone()
            base_sku = srow["sku_code"] or ""
            new_sku_code = base_sku
            if ut == "ตัว":
                new_sku_code = re.sub(r"-PN(-|$)", r"-UN\1", base_sku)
            elif ut == "แผง":
                new_sku_code = re.sub(r"-UN(-|$)", r"-PN\1", base_sku)
            print(f"  {code} {unit} → CREATE (clone pid {srow['id']}) "
                  f"id={nextid}\n"
                  f"      name='{tname}'  unit_type={srow['unit_type']} "
                  f"packaging={ut} sku_code={new_sku_code}")
            new_rows.append((code, unit, tname, srow, ut, new_sku_code,
                             nextid))
        else:
            print(f"  {code} {unit} → CREATE MINIMAL (no sibling) "
                  f"id={nextid}\n"
                  f"      name='{tname}'  unit_type={ut} "
                  f"sku_code=(FLAG: derive manually)  ⚠ review")
            new_rows.append((code, unit, tname, None, ut, None,
                             nextid))
        nextid += 1

    if not a.apply:
        print("\nDRY-RUN — review the CREATE rows above, then --apply "
              "(unique backup first).")
        conn.close()
        return 0

    conn.execute("BEGIN")
    try:
        # mig 087: packaging → packaging_th + packaging_short
        from sku_code_utils import PACKAGING_SHORT
        made = {}
        for code, unit, tname, srow, ut, scode, nid in new_rows:
            pkg_short = PACKAGING_SHORT.get(ut) if ut else None
            if srow is not None:
                cols = [c for c in pcols]
                vals = {c: srow[c] for c in cols}
                vals.update(id=nid, product_name=tname,
                            packaging_th=ut, packaging_short=pkg_short,
                            sku_code=scode, is_active=1)
                conn.execute(
                    f"INSERT INTO products ({','.join(cols)}) VALUES "
                    f"({','.join('?' * len(cols))})",
                    [vals[c] for c in cols])
            else:
                conn.execute(
                    "INSERT INTO products (id,product_name,unit_type,"
                    "packaging_th,packaging_short,sku_code,is_active) "
                    "VALUES (?,?,?,?,?,?,1)",
                    (nid, tname, ut, ut, pkg_short, f"NEW-REVIEW-{nid}"))
            made[tname] = nid
        # resolve every action to a target pid
        for code, unit, tname, mode, sib in actions:
            tid = name2id.get(tname) or made.get(tname)
            old = conn.execute("SELECT product_id FROM product_code_mapping "
                               "WHERE bsn_code=?", (code,)).fetchone()
            touched = {tid}
            if old:
                touched.add(old["product_id"])
                conn.execute("UPDATE product_code_mapping SET product_id=?,"
                             "is_ignored=0 WHERE bsn_code=?", (tid, code))
            else:
                conn.execute("INSERT INTO product_code_mapping (bsn_code,"
                             "bsn_name,product_id,is_ignored) VALUES "
                             "(?,?,?,0)", (code, code, tid))
            for t in LEDGER:
                for rr in conn.execute(f"SELECT DISTINCT product_id FROM "
                                       f"{t} WHERE bsn_code=?", (code,)):
                    if rr[0] is not None:
                        touched.add(rr[0])
                conn.execute(f"UPDATE {t} SET product_id=? WHERE bsn_code=?",
                             (tid, code))
            if unit:
                if conn.execute("SELECT 1 FROM unit_conversions WHERE "
                                "product_id=? AND bsn_unit=?",
                                (tid, unit)).fetchone():
                    conn.execute("UPDATE unit_conversions SET ratio=1 WHERE "
                                 "product_id=? AND bsn_unit=?", (tid, unit))
                else:
                    conn.execute("INSERT INTO unit_conversions (product_id,"
                                 "bsn_unit,ratio) VALUES (?,?,1)",
                                 (tid, unit))
            for pid in touched:
                if pid is None:
                    continue
                conn.execute("DELETE FROM stock_levels WHERE product_id=?",
                             (pid,))
                conn.execute(
                    "INSERT INTO stock_levels (product_id,quantity) "
                    "SELECT ?,COALESCE(SUM(quantity_change),0) FROM "
                    "transactions WHERE product_id=?", (pid, pid))
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1

    bad = 0
    for code, unit, tname, mode, sib in actions:
        tid = name2id.get(tname) or made.get(tname)
        m = conn.execute("SELECT product_id FROM product_code_mapping WHERE "
                         "bsn_code=?", (code,)).fetchone()[0]
        if m != tid:
            bad += 1
    print(f"\nAPPLIED. {len(actions)} remapped, "
          f"{len(new_rows)} new SKU(s) created. mismatches {bad} (want 0)")
    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
