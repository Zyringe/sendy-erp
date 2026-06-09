#!/usr/bin/env python3
"""Re-parse all products and emit a review CSV for round-1 normalization.

Reads every row in `products`, runs the canonical name parser
(`parse_sku_names.parse_name`), and proposes:
  - proposed_name (display) — pack_variant=1 suppressed, junk preserved
  - proposed_sku_code — via build_sku_code (10-slot rule, no material,
                        pack_variant=1 suppressed)
  - structured columns broken out old vs new
  - junk_flags column flagging suspicious patterns
  - parse_drift boolean catching cases where re-parse disagrees with stored
    structured columns even when no audit suggestion existed

Output CSV (UTF-8-BOM for Excel Thai support) lands in
Operations/05_analysis-reports/normalize_round1_<date>.csv by default.

User reviews CSV in Excel, sets `approve="Y"` on rows to apply, then
apply_normalize_round1.py runs (Session 2).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Allow running both as `python sendy_erp/scripts/...` and via the
# subprocess in tests (cwd=sendy_erp, sys.path lacks scripts/).
REPO = Path(__file__).resolve().parent.parent
INVENTORY_APP = REPO / "inventory_app"
SCRIPTS = REPO / "scripts"
for p in (str(INVENTORY_APP), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from sku_code_utils import PACKAGING_SHORT, build_sku_code  # noqa: E402
from parse_sku_names import parse_name, build_proposed_name  # noqa: E402

DEFAULT_DB = REPO / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT = REPO.parent / "Operations" / "05_analysis-reports" / \
              f"normalize_round1_{date.today().isoformat()}.csv"
AUDIT_CSV = Path(os.path.expanduser(
    "~/Downloads/worsawat_audit_workbook_2026-05-27.xlsx - sku-breakdown.csv"
))


CSV_COLUMNS = [
    "product_id", "current_sku_code", "proposed_sku_code",
    "current_name", "proposed_name",
    "category", "brand_short",
    "series_old", "series_new",
    "model_old", "model_new",
    "size_old", "size_new",
    "color_code_old", "color_code_new",
    "packaging_th_old", "packaging_th_new",
    "packaging_short_old", "packaging_short_new",
    "condition_old", "condition_new",
    "pack_variant_old", "pack_variant_new",
    "junk_flags", "parse_drift",
    "audit_2026_05_27_suggestion",
    "needs_change", "approve",
]


def _build_display_name(parsed: dict, pack_variant: str | None) -> str:
    """build_proposed_name + pack_variant suffix when >= 2."""
    base = build_proposed_name(parsed)
    if pack_variant and str(pack_variant) != "1":
        return f"{base} {pack_variant}"
    return base


_JUNK_TRAILING_KHEED = re.compile(r"\(\s*\d+\s*ขีด\s*\)\s*$")
_DOUBLE_SPACE = re.compile(r"  +")
_SINGLE_LETTER_SERIES = re.compile(r"^[A-Z]$")


def _junk_flags(product_name: str, series: str | None) -> list[str]:
    flags = []
    if _JUNK_TRAILING_KHEED.search(product_name or ""):
        flags.append("trailing_kheed_suffix")
    if product_name and product_name != product_name.strip():
        flags.append("trailing_ws")
    if _DOUBLE_SPACE.search(product_name or ""):
        flags.append("double_space")
    if series and _SINGLE_LETTER_SERIES.fullmatch(series):
        flags.append("single_letter_series")
    return flags


def _norm(v) -> str:
    """Normalize for old-vs-new equality: empty/None → ''; strip."""
    if v is None:
        return ""
    s = str(v).strip()
    return s


def _suppressed_pv(pv: str | None) -> str:
    """Hide pack_variant when '1' (default); otherwise return string form."""
    if pv is None:
        return ""
    s = str(pv).strip()
    if not s or s == "1":
        return ""
    return s


def _load_audit_suggestions(path: Path) -> dict[int, str]:
    """Read column-format-reference audit CSV and return product_id → suggestion."""
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                pid = int(row["product_id"])
            except (KeyError, ValueError, TypeError):
                continue
            sug = (row.get("suggestions") or "").strip()
            if sug:
                out[pid] = sug
    return out


def _load_color_codes(conn) -> dict:
    return {r["code"]: r["name_th"] for r in conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY length(code) DESC"
    )}


def _load_brands(conn):
    rows = conn.execute(
        "SELECT id, code, name, name_th, short_code FROM brands"
    ).fetchall()
    brands_by_id = {r["id"]: dict(r) for r in rows}
    all_brand_tokens = []
    token_to_brand = {}
    for r in rows:
        for k in ("name", "name_th", "short_code"):
            v = r[k]
            if v:
                all_brand_tokens.append(v)
                token_to_brand[v] = r["name"]
    return brands_by_id, all_brand_tokens, token_to_brand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("NORMALIZE_DB_PATH",
                                                 str(DEFAULT_DB))))
    ap.add_argument("--audit-csv", type=Path, default=AUDIT_CSV)
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    color_codes = _load_color_codes(conn)
    brands_by_id, all_brand_tokens, token_to_brand = _load_brands(conn)
    audit_sugs = _load_audit_suggestions(args.audit_csv)

    rows = conn.execute("""
        SELECT p.id, p.product_name, p.brand_id,
               p.series, p.model, p.size, p.color_code,
               p.packaging_th, p.packaging_short,
               p.condition, p.pack_variant, p.sku_code,
               p.sub_category_short_code,
               b.short_code AS brand_short_code,
               c.short_code AS cat_short_code,
               c.name_th    AS cat_name_th
          FROM products p
          LEFT JOIN brands b      ON b.id = p.brand_id
          LEFT JOIN categories c  ON c.id = p.category_id
         ORDER BY p.id
    """).fetchall()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        n_changed = 0
        n_junk = 0
        for r in rows:
            brand_rec = brands_by_id.get(r["brand_id"]) if r["brand_id"] else None
            parsed = parse_name(r["product_name"] or "", brand_rec,
                                color_codes, all_brand_tokens, token_to_brand)

            # Map parsed dict → DB-shape columns
            pkg_th_new = parsed.get("packaging") or ""
            pkg_short_new = PACKAGING_SHORT.get(pkg_th_new) if pkg_th_new else ""
            pv_raw = parsed.get("pack_variant") or ""
            pv_new = _suppressed_pv(pv_raw)

            # Proposed name uses suppressed pack_variant (>= 2 shown, 1 hidden)
            proposed_name = _build_display_name(parsed, pv_new or None)

            # Proposed sku_code via build_sku_code with new schema
            proposed_sku_code = build_sku_code({
                "cat_short_code": r["cat_short_code"],
                "sub_category_short_code": r["sub_category_short_code"],
                "brand_short_code": r["brand_short_code"],
                "model": parsed.get("model") or "",
                "size": parsed.get("size") or "",
                "series": parsed.get("series") or "",
                "color_code": parsed.get("color_code") or "",
                "packaging_short": pkg_short_new,
                "condition": parsed.get("condition") or "",
                "pack_variant": pv_new,
            })

            junk = _junk_flags(r["product_name"], parsed.get("series"))
            if junk:
                n_junk += 1

            # parse_drift: parser disagrees with stored structured cols on any field
            drift = any([
                _norm(r["series"]) != _norm(parsed.get("series")),
                _norm(r["model"]) != _norm(parsed.get("model")),
                _norm(r["size"]) != _norm(parsed.get("size")),
                _norm(r["color_code"]) != _norm(parsed.get("color_code")),
                _norm(r["packaging_th"]) != _norm(pkg_th_new),
                _norm(r["condition"]) != _norm(parsed.get("condition")),
                _norm(r["pack_variant"]) != _norm(pv_raw),
            ])

            needs_change = (
                _norm(proposed_name) != _norm(r["product_name"]) or
                _norm(proposed_sku_code) != _norm(r["sku_code"])
            )
            if needs_change:
                n_changed += 1

            w.writerow({
                "product_id": r["id"],
                "current_sku_code": r["sku_code"] or "",
                "proposed_sku_code": proposed_sku_code,
                "current_name": r["product_name"] or "",
                "proposed_name": proposed_name,
                "category": r["cat_name_th"] or "",
                "brand_short": r["brand_short_code"] or "",
                "series_old": r["series"] or "",
                "series_new": parsed.get("series") or "",
                "model_old": r["model"] or "",
                "model_new": parsed.get("model") or "",
                "size_old": r["size"] or "",
                "size_new": parsed.get("size") or "",
                "color_code_old": r["color_code"] or "",
                "color_code_new": parsed.get("color_code") or "",
                "packaging_th_old": r["packaging_th"] or "",
                "packaging_th_new": pkg_th_new,
                "packaging_short_old": r["packaging_short"] or "",
                "packaging_short_new": pkg_short_new,
                "condition_old": r["condition"] or "",
                "condition_new": parsed.get("condition") or "",
                "pack_variant_old": r["pack_variant"] or "",
                "pack_variant_new": pv_new,
                "junk_flags": ",".join(junk),
                "parse_drift": "Y" if drift else "",
                "audit_2026_05_27_suggestion": audit_sugs.get(r["id"], ""),
                "needs_change": "Y" if needs_change else "",
                "approve": "",
            })

    print(f"Wrote {len(rows)} rows → {args.output}")
    print(f"  needs_change: {n_changed}")
    print(f"  with junk flags: {n_junk}")
    print(f"  audit cross-ref rows: {len(audit_sugs)}")
    conn.close()


if __name__ == "__main__":
    main()
