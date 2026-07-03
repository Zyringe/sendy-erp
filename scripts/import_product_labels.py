#!/usr/bin/env python3
"""One-time import of the ป้ายสินค้า (product label) master Excel into Sendy.

Phase 1 of projects/product-label-printing/plan.md. Writes:
    product_labels        — one row per label (see migration 127)
    label_company_block   — one seeded row (the ~constant boilerplate block)

Source: /Volumes/ZYRINGE_128/Sendai-Boonsawat/Barcode_GoDEX/barcode all (put) edit.xls
Sheet:  "Barcode (name) (12.12.60)" — 18 columns, always in this order:
    No, ลำดับ, รหัสบาร์โค้ด, ชื่อสินค้า, ตราสินค้า, วิธีใช้, ข้อแนะนำ, ผู้จัดจำหน่าย,
    ราคา, บรรจุ, ขนาด, ที่อยู่, นำเข้าโดย, ที่อยู่ผู้นำเข้า1, ที่อยู่ผู้นำเข้า2,
    ผลิต1, ผลิต2, บรรจุลัง
Columns are matched by POSITION (not header text) — the sheet's Thai headers
are not guaranteed to match these labels exactly (spacing/typos).

Cleaning (decisions D5 in the plan — import-and-flag, never drop a barcode):
  - Strip a leading "<label> : " prefix off cells that carry it (only the
    columns known to have this quirk: ตราสินค้า/วิธีใช้/ข้อแนะนำ/บรรจุ/ขนาด/
    ผู้จัดจำหน่าย/ราคา).
  - Normalize barcode cells (Excel reads numeric barcodes as float -> strip
    the trailing ".0").
  - needs_review=1 + a review_note for barcodes whose length != 13 or that
    duplicate another row's barcode. Row is still imported.
  - Skip rows with a blank product name.

Re-runnable: clears product_labels + label_company_block first, so re-running
on the same source is safe (this is a one-time master import, not a merge).

Default is DRY RUN (parses + prints counts, no writes). Pass --commit to write.

Usage:
    /Users/putty/.virtualenvs/erp/bin/python scripts/import_product_labels.py
    /Users/putty/.virtualenvs/erp/bin/python scripts/import_product_labels.py --commit
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inventory_app"))
import config  # noqa: E402  (resolves DATABASE_PATH via DATA_DIR, like the app)

DEFAULT_SOURCE = (
    "/Volumes/ZYRINGE_128/Sendai-Boonsawat/Barcode_GoDEX/barcode all (put) edit.xls"
)
DEFAULT_SHEET = "Barcode (name) (12.12.60)"

# Column order in the source sheet (positional — see module docstring).
COLUMNS = [
    "No", "ลำดับ", "รหัสบาร์โค้ด", "ชื่อสินค้า", "ตราสินค้า", "วิธีใช้", "ข้อแนะนำ",
    "ผู้จัดจำหน่าย", "ราคา", "บรรจุ", "ขนาด", "ที่อยู่", "นำเข้าโดย",
    "ที่อยู่ผู้นำเข้า1", "ที่อยู่ผู้นำเข้า2", "ผลิต1", "ผลิต2", "บรรจุลัง",
]

# Thai field-label prefixes known to be embedded in some cells, e.g. a
# "ตราสินค้า" cell literally containing the text "ตราสินค้า : GOLDEN LION".
# Only strip a column's OWN label — never guess prefixes for columns not
# confirmed to carry this quirk.
FIELD_LABELS = {
    "ตราสินค้า": "ตราสินค้า",
    "วิธีใช้": "วิธีใช้",
    "ข้อแนะนำ": "ข้อแนะนำ",
    "บรรจุ": "บรรจุ",
    "ขนาด": "ขนาด",
    "ผู้จัดจำหน่าย": "ผู้จัดจำหน่าย",
    "ราคา": "ราคา",
}


# ── Cleaning helpers (unit-tested in tests/test_product_labels_import.py) ──

def strip_field_prefix(value, label: str) -> str:
    """Strip a leading '<label> : ' (or '<label>: ', '<label> ：') prefix.

    Blank/NaN -> ''. Only strips when the value actually starts with the
    label; otherwise returns the value stripped of surrounding whitespace.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    pattern = r"^" + re.escape(label) + r"\s*[:：]\s*"
    return re.sub(pattern, "", text).strip()


def normalize_barcode(value) -> str:
    """Normalize a barcode cell to a plain digit string.

    Handles the common Excel artifact where a numeric-looking barcode is
    read back as a float (e.g. 8850124001234.0 -> '8850124001234').
    Blank/NaN -> ''.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value == int(value):
            return str(int(value))
        return str(value).strip()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    return text


def is_blank_name(value) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


def flag_barcode_length(barcode: str) -> Optional[str]:
    """Return a review_note fragment if barcode length != 13, else None.
    Blank barcodes are not flagged here (nothing to check yet)."""
    if not barcode:
        return None
    if len(barcode) != 13:
        return f"barcode length {len(barcode)} (expected 13)"
    return None


def find_duplicate_barcodes(barcodes) -> set:
    """Given an iterable of normalized barcodes (blank allowed), return the
    set of non-blank values that occur more than once."""
    counts = Counter(b for b in barcodes if b)
    return {b for b, n in counts.items() if n > 1}


def most_common_value(values, label: Optional[str] = None) -> Optional[str]:
    """Mode of a column's non-blank values, optionally prefix-stripped first.
    Used to seed label_company_block's single row from the ~constant columns."""
    cleaned = [strip_field_prefix(v, label) if label else str(v).strip()
               for v in values]
    cleaned = [v for v in cleaned if v and v.lower() != "nan"]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


# ── Row-level transform ─────────────────────────────────────────────────────

def clean_rows(raw_rows):
    """Transform raw sheet rows (list of dict, keyed by COLUMNS) into
    product_labels-ready dicts.

    Returns (rows, skipped_blank_count).
    """
    barcodes = [normalize_barcode(r.get("รหัสบาร์โค้ด")) for r in raw_rows]
    dup_barcodes = find_duplicate_barcodes(barcodes)

    rows = []
    skipped = 0
    for r, barcode in zip(raw_rows, barcodes):
        if is_blank_name(r.get("ชื่อสินค้า")):
            skipped += 1
            continue

        reasons = []
        len_reason = flag_barcode_length(barcode)
        if len_reason:
            reasons.append(len_reason)
        if barcode and barcode in dup_barcodes:
            reasons.append("duplicate barcode")

        legacy_no = r.get("No")
        legacy_no = "" if legacy_no is None else str(legacy_no).strip()
        if legacy_no.endswith(".0"):
            legacy_no = legacy_no[:-2]

        rows.append({
            "barcode": barcode,
            "product_name": str(r["ชื่อสินค้า"]).strip(),
            "brand": strip_field_prefix(r.get("ตราสินค้า"), FIELD_LABELS["ตราสินค้า"]),
            "usage_th": strip_field_prefix(r.get("วิธีใช้"), FIELD_LABELS["วิธีใช้"]),
            "warning_th": strip_field_prefix(r.get("ข้อแนะนำ"), FIELD_LABELS["ข้อแนะนำ"]),
            "packaging_th": strip_field_prefix(r.get("บรรจุ"), FIELD_LABELS["บรรจุ"]),
            "size_th": strip_field_prefix(r.get("ขนาด"), FIELD_LABELS["ขนาด"]),
            "legacy_no": legacy_no or None,
            "needs_review": 1 if reasons else 0,
            "review_note": "; ".join(reasons) if reasons else None,
        })
    return rows, skipped


def build_company_block(raw_rows):
    """Mode-derive the single label_company_block row from the ~constant
    columns. price_line_th is NOT read from the sheet's own 'ราคา' column
    (that carries historic per-product prices) — it is always the fixed
    text per decision D-price (own-brand B2B price varies per customer)."""
    return {
        "distributor_th": most_common_value(
            (r.get("ผู้จัดจำหน่าย") for r in raw_rows), FIELD_LABELS["ผู้จัดจำหน่าย"]),
        "importer_th": most_common_value(r.get("นำเข้าโดย") for r in raw_rows),
        "address_th": most_common_value(r.get("ที่อยู่") for r in raw_rows),
        "importer_addr1_th": most_common_value(r.get("ที่อยู่ผู้นำเข้า1") for r in raw_rows),
        "importer_addr2_th": most_common_value(r.get("ที่อยู่ผู้นำเข้า2") for r in raw_rows),
        "country_th": most_common_value(r.get("ผลิต1") for r in raw_rows),
        "quality_th": most_common_value(r.get("ผลิต2") for r in raw_rows),
    }


# ── DB I/O ───────────────────────────────────────────────────────────────────

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_sheet(source: str, sheet: str):
    """Read the source .xls sheet and return a list of dict rows keyed by
    COLUMNS (matched positionally, header row skipped)."""
    import pandas as pd
    df = pd.read_excel(source, sheet_name=sheet, header=0, dtype=object)
    if len(df.columns) < len(COLUMNS):
        raise ValueError(
            f"sheet has {len(df.columns)} columns, expected >= {len(COLUMNS)}"
        )
    df = df.iloc[:, :len(COLUMNS)]
    df.columns = COLUMNS
    return df.to_dict("records")


def write_import(conn, rows, company_block):
    conn.execute("DELETE FROM product_labels")
    conn.execute("DELETE FROM label_company_block")

    conn.executemany(
        """
        INSERT INTO product_labels
            (barcode, product_name, brand, usage_th, warning_th, packaging_th,
             size_th, legacy_no, needs_review, review_note)
        VALUES (:barcode, :product_name, :brand, :usage_th, :warning_th,
                :packaging_th, :size_th, :legacy_no, :needs_review, :review_note)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO label_company_block
            (distributor_th, importer_th, address_th, importer_addr1_th,
             importer_addr2_th, country_th, quality_th)
        VALUES (:distributor_th, :importer_th, :address_th, :importer_addr1_th,
                :importer_addr2_th, :country_th, :quality_th)
        """,
        company_block,
    )
    conn.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--sheet", default=DEFAULT_SHEET)
    ap.add_argument("--db", default=config.DATABASE_PATH)
    ap.add_argument("--commit", action="store_true",
                     help="write to the DB (default is dry run)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.source):
        print(f"Source not found: {args.source}", file=sys.stderr)
        return 2

    raw_rows = load_sheet(args.source, args.sheet)
    rows, skipped = clean_rows(raw_rows)
    company_block = build_company_block(raw_rows)
    flagged = [r for r in rows if r["needs_review"]]

    print(f"source rows:     {len(raw_rows)}")
    print(f"skipped (blank): {skipped}")
    print(f"imported rows:   {len(rows)}")
    print(f"needs_review:    {len(flagged)}")
    for r in flagged:
        print(f"  - {r['barcode'] or '(blank)'}: {r['review_note']} — {r['product_name']}")
    brands = Counter(r["brand"] for r in rows if r["brand"])
    print(f"brands: {dict(brands)}")
    print(f"company_block: {company_block}")

    if not args.commit:
        print("\nDRY RUN — no writes. Pass --commit to write.")
        return 0

    conn = get_connection(args.db)
    write_import(conn, rows, company_block)
    conn.close()
    print(f"\nWrote {len(rows)} product_labels rows + 1 label_company_block row to {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
