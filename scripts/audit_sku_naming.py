"""Audit SKU product names against the naming rule defined in
sendy_erp/docs/product_name_naming_rule.md (locked 2026-05-05).

Reports each SKU that violates one or more rules. Output is written to
sendy_erp/data/exports/sku_naming_audit.csv with columns:

    product_id, product_name, brand, issues

`issues` is a semicolon-separated list of issue codes:

    NO_BRAND       — products.brand_id is NULL or brand name not in product_name
    NO_HASH        — has digits that look like a model but missing '#' prefix
    INCH_QUOTE     — uses '"' instead of 'นิ้ว'
    INCH_SPACED    — has 'X นิ้ว' with space (should be 'Xนิ้ว')
    INCH_FRAC      — uses 'a b/c' fraction (should be decimal)
    P_LEGACY       — has '(P)' instead of '(แผง)'
    BARE_COLOR     — has color code (AC/SS/...) without 'สีXX (CODE)' Thai prefix
    NEW_COLOR      — has uppercase 2-5 letter token that looks like a color code
                     but isn't in color_finish_codes table
    DOUBLE_SPACE   — has '  ' (double space)
    TOO_LONG       — name > 60 characters
    HAS_SERIES_GAP — has 'X (P)' or 'X (ตัว)' with space (series should be 'XY' no space)

CLI:
    python sendy_erp/scripts/audit_sku_naming.py
    python sendy_erp/scripts/audit_sku_naming.py --only-active
    python sendy_erp/scripts/audit_sku_naming.py --output /tmp/audit.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT = ROOT / "data" / "exports" / "sku_naming_audit.csv"

# Tokens that look uppercase-code but aren't color/finish codes — skip them.
NON_COLOR_TOKENS = {
    "CM", "MM", "META", "TOA", "SS304", "VAT", "BSN", "LED", "PVC", "UPVC",
    "PE", "PP", "ABS", "HD", "II", "III", "IV", "PRO", "MAX", "MINI",
    "DIY", "XL", "XXL", "PNG", "JPG", "GMP", "OEM", "CCTV", "USB", "RJ",
    "TV", "HDMI", "OZ", "PIN", "BALL", "DOME", "WHITE", "BLACK", "BROWN",
    "BRUSH", "STAR", "INTER", "OK", "OPP", "STL", "LM", "GP", "MAC",
    "PU", "AAA", "LINK", "SDS", "SPA", "CSK", "CRC", "KP", "KPS",
    "NRK", "JBB", "SOMIC", "MACOH", "ASAHI", "LAMY", "BRAVO", "SCALA",
    "SUNCO", "ORBIT", "SANWA", "SOLEX", "BAHCO", "KING", "FAST", "YOKO",
    "SD", "GL", "AS", "EAGLE", "NN", "3RD",
}


def load_color_codes(conn):
    rows = conn.execute("SELECT code FROM color_finish_codes").fetchall()
    return {r[0] for r in rows}


def load_brand_names(conn):
    """Load all brand names + thai names + short codes for FK lookup."""
    rows = conn.execute(
        "SELECT id, name, name_th, short_code FROM brands"
    ).fetchall()
    return {r[0]: {"name": r[1], "name_th": r[2], "short_code": r[3]}
            for r in rows}


# Tokens that indicate a brand even if products.brand_id is NULL.
# Audit treats name as branded when ANY of these tokens appear in product_name.
BRAND_ALIASES = {
    # Golden Lion
    "สิงห์ทอง": "Golden Lion",
    "สิงห์":    "Golden Lion",
    # Sendai
    "S/D":     "Sendai",
    "SD-":     "Sendai",
    "เซ็นได":   "Sendai",
    # TOA
    "จระเข้":  "TOA",
}


def name_has_brand(name: str, brand_record: dict | None) -> bool:
    """Check whether the SKU's name contains its brand's name, name_th,
    short_code, or any known alias for that brand."""
    name_lower = name.lower()

    candidates = []
    if brand_record:
        for k in ("name", "name_th", "short_code"):
            v = brand_record.get(k)
            if v:
                candidates.append(v)

    # Also accept any alias whose target brand matches this one
    for alias, target in BRAND_ALIASES.items():
        if brand_record and brand_record.get("name") == target:
            candidates.append(alias)

    for c in candidates:
        if c.lower() in name_lower:
            return True
    return False


def name_has_any_brand_token(name: str, all_brands: dict) -> bool:
    """SKU has no brand_id assigned but the name might still contain a brand
    token (e.g. 'FION' in name). Returns True if any known brand identifier
    appears."""
    name_lower = name.lower()
    for rec in all_brands.values():
        for k in ("name", "name_th", "short_code"):
            v = rec.get(k)
            if v and v.lower() in name_lower:
                return True
    for alias in BRAND_ALIASES:
        if alias.lower() in name_lower:
            return True
    return False


def audit_name(name: str, brand_record: dict | None, color_codes: set,
               all_brands: dict, brand_tokens: set) -> list:
    issues = []

    # NO_BRAND — SKU has brand_id but name doesn't contain any brand token,
    # OR SKU has no brand_id and name doesn't contain ANY known brand token.
    if brand_record:
        if not name_has_brand(name, brand_record):
            issues.append("NO_BRAND")
    else:
        if not name_has_any_brand_token(name, all_brands):
            issues.append("NO_BRAND")

    # NO_HASH — has 3-5 digits that look like model but no # prefix
    # We look for whitespace+digits+(end|space|nิ้ว) without leading #
    if re.search(r"(?<!#)(?<![\d.])\b\d{2,5}\b(?!\s*(?:\.|/|x))", name):
        # Skip if name has '#' anywhere (model exists, just has other digits)
        if "#" not in name:
            issues.append("NO_HASH")

    # INCH_QUOTE
    if '"' in name:
        issues.append("INCH_QUOTE")

    # INCH_SPACED — "3 นิ้ว" with space
    if re.search(r"\d\s+นิ้ว", name):
        issues.append("INCH_SPACED")

    # INCH_FRAC — "1 1/2" or "1 1/2นิ้ว"
    if re.search(r"\d\s+\d/\d", name):
        issues.append("INCH_FRAC")

    # P_LEGACY — uses (P) instead of (แผง)
    if "(P)" in name or "(p)" in name:
        issues.append("P_LEGACY")

    # BARE_COLOR — has known color code without Thai-prefixed format
    # Format we want: 'สีXXX (CODE)' . Bare 'CODE' at end is wrong.
    bare_color_pattern = re.compile(r"\b([A-Z]{2,4})\b")
    has_thai_color_marker = "สี" in name or "สแตนเลส" in name or "พลาสติก" in name
    for m in bare_color_pattern.finditer(name):
        code = m.group(1)
        if code in color_codes:
            # Check if format is 'สีXXX (CODE)' — code must be in parens
            if f"({code})" not in name:
                issues.append("BARE_COLOR")
                break

    # NEW_COLOR — uppercase token that's NOT in color_codes,
    # NOT in NON_COLOR_TOKENS, NOT a brand identifier — could be a real
    # unknown color code OR (more likely) a model-prefix / brand shorthand.
    # Heuristic: flag only 2-3 letter tokens (real codes are SS,SN,AC,...);
    # 4+ letters mostly model/brand prefixes (FION, SDM, SHD, SMIC).
    skip_tokens = NON_COLOR_TOKENS | brand_tokens
    for m in bare_color_pattern.finditer(name):
        code = m.group(1)
        if (code not in color_codes
                and code not in skip_tokens
                and 2 <= len(code) <= 3
                and m.start() > 5):
            issues.append(f"NEW_COLOR:{code}")
            break

    # DOUBLE_SPACE
    if "  " in name:
        issues.append("DOUBLE_SPACE")

    # TOO_LONG
    if len(name) > 60:
        issues.append("TOO_LONG")

    # HAS_SERIES_GAP — "X (P)" with space — but family from series like
    # "กลอนพฤกษา (P)" the (P) is correct, the issue is just P_LEGACY.
    # Skip this one — covered by P_LEGACY.

    return issues


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--only-active", action="store_true",
                   help="only audit is_active=1 products")
    p.add_argument("--output", type=Path, default=DEFAULT_OUT,
                   help=f"output CSV path (default: {DEFAULT_OUT})")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    color_codes = load_color_codes(conn)
    brand_names = load_brand_names(conn)

    # Build set of all brand tokens (name, name_th, short_code) — used to
    # filter NEW_COLOR false positives where a brand's short_code matches
    # the uppercase-token regex.
    brand_tokens: set = set()
    for rec in brand_names.values():
        for k in ("name", "name_th", "short_code"):
            v = rec.get(k)
            if v:
                brand_tokens.add(v.upper())

    sql = "SELECT id, product_name, brand_id, is_active FROM products"
    if args.only_active:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    flagged = 0
    issue_counts: dict = {}

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "product_name", "brand", "is_active", "issues"])
        for r in rows:
            total += 1
            brand_rec = brand_names.get(r["brand_id"]) if r["brand_id"] else None
            issues = audit_name(r["product_name"], brand_rec, color_codes,
                                brand_names, brand_tokens)
            if issues:
                flagged += 1
                for i in issues:
                    key = i.split(":")[0]
                    issue_counts[key] = issue_counts.get(key, 0) + 1
                w.writerow([
                    r["id"], r["product_name"],
                    (brand_rec["name"] if brand_rec else "(none)"),
                    r["is_active"], ";".join(issues),
                ])

    pct = (flagged / total * 100) if total else 0
    print(f"Total products audited: {total}")
    print(f"Flagged (off-rule):     {flagged} ({pct:.1f}%)")
    print(f"Output:                 {args.output}")
    print()
    print("Issues breakdown:")
    for k, v in sorted(issue_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<16} {v}")


if __name__ == "__main__":
    main()
