"""DEPRECATED: one-off from 2026-05-19. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply reviewed per-unit mapping overrides (mig 061) + re-attribute and
re-sync the affected ledger rows.

Input: the CSV produced by scripts/export_multiunit_candidates.py with
override_unit + override_product_id filled ONLY for the rows that are a
real ตัว/แผง-style SKU split.

Per filled (bsn_code, override_unit → target_pid):
  1. normalise override_unit (bsn_units.normalize_unit) — must match the
     already-normalised ledger unit.
  2. upsert the (bsn_code, bsn_unit) override row → target_pid.
  3. find every sales/purchase row of (code, that unit) currently on a
     product != target → re-attribute, applying the canonical re-sync
     pattern from models.update_unit_conversion_ratio (delete the derived
     'BSN %' ledger rows + paired 'ประวัติขาย%' history-IN by doc_no,
     reset synced_to_stock=0, _sync_bsn_to_stock both tables).
  4. recalc stock_levels (literal pid) + WACC for every old & target pid.

Only codes present in the CSV with BOTH override columns filled are
touched. Dry-run by default (prints per-code old→new + stock deltas).
--apply commits. A unique backup is taken before any write.

RUN ORDER: migration 061 → backend deploy → Put marks overrides →
THIS script → THEN scripts/rebuild_opening_balance_v2.py (this script only
re-attributes + re-syncs; it does NOT back-solve opening — the opening
rebuild finalises stock@cutoff vs the physical-count CSV).
Never run concurrently with the opening-balance rebuild.

  python scripts/apply_unit_aware_remap.py --csv reviewed.csv [--apply]
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402
import bsn_units  # noqa: E402
import models  # noqa: E402

LEDGER = ("sales_transactions", "purchase_transactions")


def _stock(conn, pid):
    r = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                      (pid,)).fetchone()
    return r[0] if r else 0


def _recalc(conn, pid):
    conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
    conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) VALUES (?, "
        "COALESCE((SELECT SUM(quantity_change) FROM transactions "
        "WHERE product_id=?), 0))", (pid, pid))


def load_overrides(csv_path):
    """Yield (bsn_code, normalized_unit, target_pid) for fully-filled rows."""
    out = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = (r.get("bsn_code") or "").strip()
            unit = (r.get("override_unit") or "").strip()
            tgt = (r.get("override_product_id") or "").strip()
            if not (code and unit and tgt):
                continue
            norm = bsn_units.normalize_unit(unit)
            if norm != unit:
                print(f"  [normalise] {code}: '{unit}' → '{norm}'")
            out.append((code, norm, int(tgt)))
    return out


def apply(conn, overrides, do_apply):
    conn.row_factory = sqlite3.Row
    affected = set()
    plan = []

    for code, unit, tgt in overrides:
        if not conn.execute("SELECT 1 FROM products WHERE id=?",
                             (tgt,)).fetchone():
            print(f"  !! target product {tgt} not found — skip {code}/{unit}")
            continue
        bsn_name = (conn.execute(
            "SELECT bsn_name FROM product_code_mapping WHERE bsn_code=? "
            "ORDER BY (bsn_unit='') DESC LIMIT 1", (code,)).fetchone()
            or [code])[0]

        # source rows of (code, unit) not already on target
        wrong = []
        for t in LEDGER:
            for rr in conn.execute(
                f"SELECT id, product_id, doc_no, synced_to_stock FROM {t} "
                f"WHERE bsn_code=? AND unit=? AND "
                f"(product_id IS NULL OR product_id<>?)",
                    (code, unit, tgt)):
                wrong.append((t, rr["id"], rr["product_id"],
                              rr["doc_no"], rr["synced_to_stock"]))
        old_pids = {w[2] for w in wrong if w[2] is not None}
        affected |= old_pids | {tgt}
        plan.append((code, unit, tgt, sorted(old_pids), len(wrong)))

        if not do_apply:
            continue

        # 1. upsert override mapping row
        conn.execute(
            "INSERT INTO product_code_mapping "
            "(bsn_code,bsn_name,product_id,is_ignored,bsn_unit) "
            "VALUES (?,?,?,0,?) "
            "ON CONFLICT(bsn_code,bsn_unit) DO UPDATE SET "
            "product_id=excluded.product_id, is_ignored=0, "
            "ignore_reason=NULL", (code, bsn_name, tgt, unit))

        # 2. canonical re-sync, scoped to (code, unit) on each old pid
        for opid in old_pids:
            conn.execute(f"""
                DELETE FROM transactions
                 WHERE product_id=? AND
                       (note LIKE 'BSN %' OR note LIKE 'ประวัติขาย%') AND
                       reference_no IN (
                         SELECT doc_no FROM sales_transactions
                          WHERE bsn_code=? AND unit=? AND product_id=?
                         UNION
                         SELECT doc_no FROM purchase_transactions
                          WHERE bsn_code=? AND unit=? AND product_id=?)
            """, (opid, code, unit, opid, code, unit, opid))

        # 3. re-attribute source rows + mark for re-sync
        for t in LEDGER:
            conn.execute(
                f"UPDATE {t} SET product_id=?, synced_to_stock=0 "
                f"WHERE bsn_code=? AND unit=?", (tgt, code, unit))

    if do_apply:
        models._sync_bsn_to_stock(conn, "sales_transactions", "sales")
        models._sync_bsn_to_stock(conn, "purchase_transactions", "purchase")
        for pid in sorted(affected):
            _recalc(conn, pid)
        for pid in sorted(affected):
            try:
                models.recalculate_product_wacc(pid, conn)
            except Exception as e:                       # noqa: BLE001
                print(f"  [warn] WACC pid {pid}: {e}")

    return plan, affected


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    overrides = load_overrides(a.csv)
    if not overrides:
        print("No fully-filled override rows (need bsn_code + override_unit "
              "+ override_product_id). Nothing to do.")
        return 0
    print(f"reviewed overrides: {len(overrides)}")

    if a.apply:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = a.db.with_name(f"inventory.pre-unitremap-{ts}.db")
        shutil.copy2(a.db, bak)
        print(f"backup → {bak.name}")

    conn = sqlite3.connect(str(a.db))
    pre = {}
    conn.row_factory = sqlite3.Row
    if a.apply:
        conn.execute("BEGIN")
    try:
        # snapshot stock before (for the delta print)
        for code, unit, tgt in overrides:
            for t in LEDGER:
                for rr in conn.execute(
                    f"SELECT DISTINCT product_id FROM {t} WHERE bsn_code=? "
                    f"AND unit=?", (code, unit)):
                    if rr[0] is not None:
                        pre.setdefault(rr[0], _stock(conn, rr[0]))
            pre.setdefault(tgt, _stock(conn, tgt))

        plan, affected = apply(conn, overrides, a.apply)

        for code, unit, tgt, old_pids, n in plan:
            print(f"  {code} / {unit}: {old_pids or '∅'} → pid {tgt} "
                  f"({n} ledger rows)")
        if a.apply:
            conn.execute("COMMIT")
            print("\nAPPLIED. stock deltas:")
            for pid in sorted(affected):
                print(f"  pid {pid}: {pre.get(pid, '?')} → {_stock(conn, pid)}")
            tot_pre = sum(v for v in pre.values())
            tot_now = sum(_stock(conn, p) for p in pre)
            print(f"  Σ stock over touched products: {tot_pre} → {tot_now} "
                  f"(should be conserved)")
            print("\nNEXT: run scripts/rebuild_opening_balance_v2.py to "
                  "finalise stock@cutoff vs the physical-count CSV.")
        else:
            print("\nDRY-RUN. Re-run with --apply (unique backup taken "
                  "automatically).")
    except Exception as e:                               # noqa: BLE001
        if a.apply:
            conn.execute("ROLLBACK")
        print(f"ROLLED BACK: {e}", file=sys.stderr)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
