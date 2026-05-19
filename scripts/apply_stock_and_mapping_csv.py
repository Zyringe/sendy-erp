"""Apply reviewed BSN mapping + unit conversions + re-sync stock from the
stock_and_mapping review CSV (the export from export_product_query.py + a
hand-added `Checked` column).

Only rows with Checked == 'TRUE' are applied. 'Done' / 'FALSE' are ignored.

Per TRUE row (grouped by product_id):
  - normalize bsn_unit via data/reference/bsn_unit_full.json (UNKNOWN -> skip
    that conversion, list it; never guess)
  - resolve ratio_to_base: (a) CSV value if >0 [flag if it disagrees with
    structural/price] -> (b) structural (==base->1, โหล->12, โหลคู่->24,
    คู่->2, ลัง->units_per_carton, กล่อง->units_per_box) -> (c) price-implied
    (median unit_price[bsn]/unit_price[base], rounded, if clean & enough txns)
    -> (d) needs-ratio list, skip conversion (no guess)
  - overwrite products.sku_code AND products.product_name from CSV
  - upsert product_code_mapping (if bsn_code present) and unit_conversions
  - ignore the CSV `stock` column; re-sync stock for affected products from
    the BSN ledger (delete BSN txns -> re-point/re-sync -> recalc stock_levels
    -> WACC). Report (do NOT auto-fix) any product that goes negative.

Dry-run by default. Use --apply to commit. Back up first (scripts/backup_db.sh).
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
MAP_JSON = ROOT / "data" / "reference" / "bsn_unit_full.json"
EXPORTS = ROOT / "data" / "exports"
DEFAULT_CSV = Path(
    "/Users/putty/Downloads/sku_code_final_2026-05-12.xlsx - stock_and_mapping (1).csv"
)

sys.path.insert(0, str(ROOT / "inventory_app"))
import sqlite3  # noqa: E402

STRUCTURAL = {"โหล": 12, "โหลคู่": 24, "คู่": 2}
PRICE_TOL = 0.15      # accept price-implied ratio within ±15% of an integer
PRICE_MIN_TXNS = 3    # need >= this many priced rows on each side


def _f(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def _median_price_by_unit(conn, product_id):
    """median unit_price per raw `unit` from sales (fallback purchase)."""
    out = {}
    for tbl in ("sales_transactions", "purchase_transactions"):
        rows = conn.execute(
            f"SELECT unit, unit_price FROM {tbl} "
            f"WHERE product_id=? AND unit_price IS NOT NULL AND unit_price>0",
            (product_id,),
        ).fetchall()
        for r in rows:
            out.setdefault((r["unit"] or "").strip(), []).append(r["unit_price"])
        if out:
            break
    return {u: statistics.median(v) for u, v in out.items() if v}


def _price_implied(conn, product_id, raw_bsn_unit, base_unit):
    """price[bsn] / price[base], rounded to nearest int if clean enough."""
    med = _median_price_by_unit(conn, product_id)
    # base price: rows sold in the product's own base unit
    base_p = med.get(base_unit)
    bsn_p = med.get((raw_bsn_unit or "").strip())
    if not base_p or not bsn_p or base_p <= 0:
        return None
    raw = bsn_p / base_p
    if raw < 1.2:
        return None
    nearest = round(raw)
    if nearest >= 2 and abs(raw - nearest) / nearest <= PRICE_TOL:
        return nearest
    return None


def resolve_ratio(conn, row, product, bsn_unit_full, base_unit):
    """Return (ratio, source, flag). ratio=None -> needs-ratio (skip conv)."""
    raw_bsn = (row.get("bsn_unit") or "").strip()
    csv_r = _f(row.get("ratio_to_base"))
    # Invariant safeguard (Put 2026-05-18): if the BSN unit IS the product's
    # base unit, the ratio is definitionally 1 — override any CSV value
    # (a CSV ≠ 1 here would corrupt stock ×ratio). Not "trust CSV vs estimate";
    # it's a definitional truth.
    if bsn_unit_full and bsn_unit_full == base_unit:
        flag = f"forced1 (CSV was {csv_r})" if (csv_r and abs(csv_r - 1) > 1e-9) else ""
        return 1, "forced-base==1", flag
    structural = None
    if bsn_unit_full in STRUCTURAL:
        structural = STRUCTURAL[bsn_unit_full]
    elif bsn_unit_full == "ลัง" and product["units_per_carton"]:
        structural = product["units_per_carton"]
    elif bsn_unit_full == "กล่อง" and product["units_per_box"]:
        structural = product["units_per_box"]
    # price-implied is informational ONLY. It is biased LOW for bulk units
    # because โหล/แพ/ลัง purchases usually get a volume discount (price per
    # pack ÷ price per piece < true count). So it must NOT override or "flag"
    # the CSV ratio (that produced false alarms on pid 398/389 — both CSV-
    # correct per Put). Kept for the FALSE-rows review report only.
    price = _price_implied(conn, product["id"], raw_bsn, base_unit)

    if csv_r and csv_r > 0:
        # Only a STRUCTURAL contradiction is a real flag (definitional /
        # pack-size). Price disagreement is expected (bulk discount) → ignore.
        flag = (f"CSV{csv_r}!=struct{structural}"
                if structural and abs(csv_r - structural) > 1e-9 else "")
        return csv_r, "csv", flag
    if structural:
        return structural, "structural", ""
    # No CSV, no structural: do NOT auto-trust price-implied (under-estimates
    # via bulk discount). Surface for Put with the price hint.
    hint = f" (price-implied≈{price}, low-confidence)" if price else ""
    return None, "needs-ratio", hint


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_CSV)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true", help="commit (default: dry-run)")
    args = p.parse_args(argv)

    if not args.csv_path.exists():
        print(f"CSV not found: {args.csv_path}", file=sys.stderr)
        return 2
    cmap = json.loads(MAP_JSON.read_text())["map"]

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    import models  # noqa: E402  (uses passed conn for the helpers we call)

    true_rows = [
        r for r in csv.DictReader(open(args.csv_path, encoding="utf-8-sig"))
        if (r.get("Checked") or "").strip() == "TRUE"
    ]

    name_sku = {}                 # pid -> (sku_code, product_name)
    mappings = []                 # (bsn_code, bsn_name, pid)
    conversions = []              # (pid, bsn_unit_full, ratio, source, flag)
    needs_ratio, unresolved_acr = [], []
    for r in true_rows:
        pid = int(r["product_id"])
        prod = conn.execute(
            "SELECT id,unit_type,units_per_box,units_per_carton FROM products WHERE id=?",
            (pid,),
        ).fetchone()
        if not prod:
            unresolved_acr.append(f"pid {pid} not found (row sku {r.get('sku')})")
            continue
        base_unit = (prod["unit_type"] or "").strip()
        name_sku[pid] = ((r.get("sku_code") or "").strip(),
                         (r.get("product_name") or "").strip())
        bsn_code = (r.get("bsn_code") or "").strip()
        bsn_name = (r.get("bsn_name") or "").strip()
        if bsn_code:
            mappings.append((bsn_code, bsn_name, pid))
        raw_unit = (r.get("bsn_unit") or "").strip()
        if not raw_unit:
            continue
        full = cmap.get(raw_unit)
        if not full:
            unresolved_acr.append(f"{raw_unit} (pid {pid}, bsn_code {bsn_code or '-'})")
            continue
        ratio, src, flag = resolve_ratio(conn, r, prod, full, base_unit)
        if ratio is None:
            needs_ratio.append(f"{raw_unit}->{full} pid {pid} bsn {bsn_code or '-'}{flag}")
            continue
        conversions.append((pid, full, float(ratio), src, flag))

    affected = set(name_sku) | {m[2] for m in mappings} | {c[0] for c in conversions}
    changed_codes = sorted({m[0] for m in mappings})

    print(f"=== TRUE rows: {len(true_rows)} | products: {len(name_sku)} ===")
    print(f"  sku_code/name overwrites : {len(name_sku)}")
    print(f"  product_code_mapping     : {len(mappings)} (codes {len(changed_codes)})")
    print(f"  unit_conversions upserts : {len(conversions)}")
    bysrc = {}
    for *_x, src, _flag in conversions:
        bysrc[src] = bysrc.get(src, 0) + 1
    print(f"     by ratio source       : {bysrc}")
    flags = [c for c in conversions if c[4]]
    print(f"  ratio disagreement flags : {len(flags)}")
    print(f"  needs-ratio (skipped)    : {len(needs_ratio)}")
    print(f"  unresolved acronym/skip  : {len(unresolved_acr)}")
    print(f"  affected products (resync): {len(affected)}")

    EXPORTS.mkdir(parents=True, exist_ok=True)
    (EXPORTS / "apply_needs_ratio.txt").write_text("\n".join(needs_ratio))
    (EXPORTS / "apply_unresolved_acronym.txt").write_text("\n".join(unresolved_acr))
    (EXPORTS / "apply_ratio_flags.txt").write_text(
        "\n".join(f"pid {c[0]} {c[1]} r={c[2]} [{c[3]}] {c[4]}" for c in flags))
    if flags[:10]:
        print("\n  sample disagreement flags:")
        for c in flags[:10]:
            print(f"    pid {c[0]} {c[1]} r={c[2]} [{c[3]}] {c[4]}")

    if not args.apply:
        print("\nDRY-RUN. Back up (scripts/backup_db.sh) then re-run with --apply.")
        print(f"Reports: {EXPORTS}/apply_needs_ratio.txt, "
              f"apply_unresolved_acronym.txt, apply_ratio_flags.txt")
        conn.close()
        return 0

    # ---- APPLY ----
    for pid, (skuc, pname) in name_sku.items():
        if skuc:
            conn.execute("UPDATE products SET sku_code=? WHERE id=?", (skuc, pid))
        if pname:
            conn.execute("UPDATE products SET product_name=? WHERE id=?", (pname, pid))
    # mig 061: product_code_mapping is keyed by (bsn_code, bsn_unit).
    # This script writes the bsn_unit='' catch-all (per-unit overrides are
    # managed via /mapping or apply_unit_aware_remap.py).
    for bsn_code, bsn_name, pid in mappings:
        conn.execute("""
            INSERT INTO product_code_mapping
                (bsn_code,bsn_name,product_id,is_ignored,bsn_unit)
            VALUES (?,?,?,0,'')
            ON CONFLICT(bsn_code,bsn_unit) DO UPDATE SET
              bsn_name=excluded.bsn_name, product_id=excluded.product_id,
              is_ignored=0, ignore_reason=NULL
        """, (bsn_code, bsn_name, pid))
    for pid, full, ratio, *_ in conversions:
        conn.execute("""
            INSERT INTO unit_conversions (product_id,bsn_unit,ratio)
            VALUES (?,?,?)
            ON CONFLICT(product_id,bsn_unit) DO UPDATE SET ratio=excluded.ratio
        """, (pid, full, ratio))

    # collect OLD product_ids attached to changed bsn_codes (their stale stock
    # must be recalced too), then re-point ledger rows to the current mapping.
    if changed_codes:
        qs = ",".join("?" * len(changed_codes))
        for tbl in ("sales_transactions", "purchase_transactions"):
            for rr in conn.execute(
                f"SELECT DISTINCT product_id FROM {tbl} "
                f"WHERE bsn_code IN ({qs}) AND product_id IS NOT NULL", changed_codes):
                affected.add(rr["product_id"])
            conn.execute(f"""
                UPDATE {tbl} SET product_id=(
                  SELECT m.product_id FROM product_code_mapping m
                  WHERE m.bsn_code={tbl}.bsn_code
                    AND m.bsn_unit IN (COALESCE({tbl}.unit,''), '')
                    AND m.product_id IS NOT NULL
                  ORDER BY (m.bsn_unit='') LIMIT 1)
                WHERE bsn_code IN ({qs})
            """, changed_codes)

    aff = sorted(p for p in affected if p)
    qa = ",".join("?" * len(aff))

    # CRITICAL: BSN ledger stores the unit ACRONYM but the reviewed
    # unit_conversions are keyed by full Thai, and _get_base_qty matches
    # unit_conversions.bsn_unit == ledger.unit. So normalise the ledger unit
    # acronym->full Thai for each affected product — but ONLY when a matching
    # full-Thai conversion exists for that product OR full == its base unit
    # (else leave the acronym so any existing acronym conv still matches; no
    # regression). Then drop the now-superseded old acronym conv rows so there
    # is a single source of truth. (Fixes the half-applied 2026-05-18 bug.)
    cmap_norm = {a: f for a, f in cmap.items() if a != f}
    for pid in aff:
        pr = conn.execute(
            "SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()
        base = (pr["unit_type"] or "").strip() if pr else ""
        fulls = {r["bsn_unit"] for r in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,))}
        for a, f in cmap_norm.items():
            if f in fulls or f == base:
                for tbl in ("sales_transactions", "purchase_transactions"):
                    conn.execute(
                        f"UPDATE {tbl} SET unit=? WHERE product_id=? AND unit=?",
                        (f, pid, a))
                conn.execute(
                    "DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
                    (pid, a))

    conn.execute(
        f"DELETE FROM transactions WHERE product_id IN ({qa}) AND note LIKE 'BSN %'", aff)
    for tbl in ("sales_transactions", "purchase_transactions"):
        conn.execute(
            f"UPDATE {tbl} SET synced_to_stock=0 WHERE product_id IN ({qa})", aff)
    models.resolve_pending_mappings(conn)              # backfills NULL pid + syncs
    models._sync_bsn_to_stock(conn, "sales_transactions", "sales")
    models._sync_bsn_to_stock(conn, "purchase_transactions", "purchase")
    for pid in aff:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
        conn.execute("""
            INSERT INTO stock_levels (product_id,quantity)
            SELECT product_id, COALESCE(SUM(quantity_change),0)
            FROM transactions WHERE product_id=?
        """, (pid,))
    conn.commit()

    neg = conn.execute(
        f"SELECT product_id,quantity FROM stock_levels "
        f"WHERE product_id IN ({qa}) AND quantity<0 ORDER BY quantity", aff).fetchall()
    for pid in aff:
        try:
            models.recalculate_product_wacc(pid, conn)
        except Exception as e:  # WACC is best-effort; never block the apply
            print(f"  [warn] WACC recalc pid {pid}: {e}")
    conn.commit()
    (EXPORTS / "apply_negative_stock.txt").write_text(
        "\n".join(f"pid {r['product_id']} qty {r['quantity']}" for r in neg))

    print(f"\nAPPLIED. resynced {len(aff)} products. "
          f"negative-stock: {len(neg)} (see apply_negative_stock.txt — NOT auto-fixed)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
