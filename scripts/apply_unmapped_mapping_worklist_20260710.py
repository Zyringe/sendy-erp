"""One-off, 2026-07-10.

Bucket-2c fix from Put's /grilling session: apply the team's reviewed
marketplace_unmapped_mapping_worklist_2026-06-21.xlsx ("unmapped" tab) —
listings with NO product mapping at all (platform_skus.internal_product_id
IS NULL). Per the file's own header, all 135 rows are currently inactive or
out-of-stock (0 are active+in-stock), so this is a completeness/historical
cleanup, not an urgent live-matching fix.

CSV columns (row 2 = header, data from row 3): #, platform, variation_id,
ชื่อ listing, variation, ratio_ปัจจุบัน, suggested_ratio, ratio_flag,
stock_online, status, suggested_pid, suggested_ชื่อ ERP, confidence(H/M/L),
evidence, ตรวจแล้ว, new_pid, new_ratio.

Only rows with ตรวจแล้ว == TRUE and a usable new_pid are applied:
  - plain integer new_pid  -> platform_skus.internal_product_id = new_pid,
    qty_per_sale = new_ratio (default 1).
  - new_pid == 'ไม่มี'      -> team confirmed no matching product exists;
    no DB write (there is nothing to set).
  - new_pid with a comma (e.g. '252,251') -> a MULTI-PRODUCT bundle. The CSV
    lists components, not the pack id. Row 43 ("ชุดฝาครอบลูกบิดประตู + แม่
    กุญแจถอดได้ SENDAI" = components 251+252) is a KNOWN existing combo pack,
    product 253 ("ชุดฝาครอบลูกบิด+กุญแจถอดได้ Sendai") already used by
    marketplace_match.py's combo-expansion mechanism. Hardcoded override:
    maps to 253, not the raw CSV text, so the existing combo machinery
    (_combo_components in marketplace_match.py) can expand it correctly.
  - ตรวจแล้ว blank (not TRUE), regardless of new_pid -> SKIPPED (unreviewed).
    36 of these rows have new_pid == 'ไม่มี' typed in without the checkbox
    ticked (an inconsistent-but-harmless data-entry habit, not an error —
    no DB action needed there either way since 'ไม่มี' never writes).

Only touches platform_skus (internal_product_id, qty_per_sale) for rows
whose CURRENT internal_product_id IS NULL (never overwrites an existing
mapping — that is a different bucket, see fix_wrong_sibling_mapping /
resync_stale_order_item_pid). Dry-run by default. --apply commits. Backs up
first (sqlite3 .backup, not cp — WAL).
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUP_DIR = ROOT / "data" / "backups"
_INPUT_DIR = Path(os.environ.get("SENDY_INPUT_DIR", os.path.expanduser("~/Downloads")))
DEFAULT_CSV = _INPUT_DIR / "marketplace_unmapped_mapping_worklist_2026-06-21.xlsx - unmapped.csv"

# Row 43 combo override: CSV says '252,251' (components), real target is the
# existing pack product 253 (see docstring).
_COMBO_OVERRIDE = {"252,251": 253, "251,252": 253}


def _parse_rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[1]
    idx = {c: i for i, c in enumerate(header)}
    out = []
    for r in rows[2:]:
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))
        if r[idx["ตรวจแล้ว"]].strip().upper() != "TRUE":
            continue
        new_pid_raw = r[idx["new_pid"]].strip()
        if new_pid_raw in ("", "ไม่มี"):
            continue
        if "," in new_pid_raw:
            pid = _COMBO_OVERRIDE.get(new_pid_raw)
            if pid is None:
                print(f"  WARNING row {r[idx['#']]}: unrecognized combo {new_pid_raw!r} — skipped")
                continue
        else:
            pid = int(new_pid_raw)
        ratio_raw = r[idx["new_ratio"]].strip()
        ratio = float(ratio_raw) if ratio_raw else 1.0
        out.append({
            "row": r[idx["#"]], "platform": r[idx["platform"]],
            "variation_id": r[idx["variation_id"]], "listing_name": r[idx["ชื่อ listing"]],
            "new_pid": pid, "new_ratio": ratio,
        })
    return out


def find_candidates(conn, csv_path):
    """Only listings that are STILL unmapped (internal_product_id IS NULL) —
    never overwrites an existing mapping."""
    out = []
    for row in _parse_rows(csv_path):
        sku = conn.execute(
            "SELECT id, internal_product_id FROM platform_skus WHERE platform=? AND variation_id=?",
            (row["platform"], row["variation_id"])).fetchone()
        if sku is None:
            print(f"  WARNING row {row['row']}: no platform_skus row for "
                  f"({row['platform']}, {row['variation_id']}) — skipped")
            continue
        if sku["internal_product_id"] is not None:
            print(f"  SKIP row {row['row']}: platform_skus id={sku['id']} already "
                  f"mapped to {sku['internal_product_id']} (not overwriting)")
            continue
        pid_exists = conn.execute("SELECT 1 FROM products WHERE id=?", (row["new_pid"],)).fetchone()
        if not pid_exists:
            print(f"  WARNING row {row['row']}: new_pid {row['new_pid']} does not exist — skipped")
            continue
        out.append({**row, "sku_id": sku["id"]})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    candidates = find_candidates(conn, args.csv)
    print(f"\n{len(candidates)} rows to apply:")
    for c in candidates:
        print(f"  row={c['row']} sku_id={c['sku_id']} {c['platform']} "
              f"{c['listing_name'][:40]!r} -> pid={c['new_pid']} ratio={c['new_ratio']}")

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to commit.")
        conn.close()
        return

    if not candidates:
        print("\nNothing to apply.")
        conn.close()
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"pre_unmapped_worklist_apply_{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(args.db))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    src.close()
    dst.close()
    print(f"\nBackup written: {backup_path}")

    conn.execute("BEGIN IMMEDIATE")
    for c in candidates:
        conn.execute(
            "UPDATE platform_skus SET internal_product_id=?, qty_per_sale=? WHERE id=?",
            (c["new_pid"], c["new_ratio"], c["sku_id"]))
    conn.commit()

    # Independent re-verify.
    bad = 0
    for c in candidates:
        row = conn.execute("SELECT internal_product_id, qty_per_sale FROM platform_skus WHERE id=?",
                            (c["sku_id"],)).fetchone()
        if row["internal_product_id"] != c["new_pid"] or row["qty_per_sale"] != c["new_ratio"]:
            bad += 1
    print(f"\nApplied {len(candidates)} rows. Verification failures (should be 0): {bad}")
    if bad:
        raise SystemExit(f"FAILED verification: {bad} rows wrong")
    conn.close()


if __name__ == "__main__":
    main()
