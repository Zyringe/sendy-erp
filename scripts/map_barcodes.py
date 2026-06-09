"""
Match barcodes from /Volumes/ZYRINGE/barcode all (put) edit.xls
to current products in inventory.db.

Outputs:
  ERP/data/exports/barcode_mapping_matched.csv   — confident matches
  ERP/data/exports/barcode_mapping_review.csv    — needs human review
  ERP/data/exports/barcode_mapping_unmatched.csv — no candidate found

Strategy:
  1) Read both sheets, dedupe by (barcode, name).
  2) Extract embedded code (#SD-XXX / #230-4 / etc).
  3) Normalize name: strip quotes, brand markers, normalize inch ('' " → นิ้ว),
     collapse spaces, lowercase.
  4) Match in priority order:
       a) exact embedded-code in product name (high confidence)
       b) exact normalized full name
       c) name token Jaccard ≥ 0.7 (review tier)
"""
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict

import xlrd

XLS_PATH = "/Volumes/ZYRINGE/barcode all (put) edit.xls"
DB_PATH  = os.path.expanduser("~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db")
OUT_DIR  = os.path.expanduser("~/Sendai-Boonsawat/sendy_erp/data/exports")

CODE_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9\-/.]*)")
INCH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:''|\")")
QUOTE_CHARS = "'\"’‘“”`"
# Brand surface forms (Thai + English) — stripped before name comparison
BRAND_REPLACEMENTS = [
    ("'sendai'", " "), ("sendai", " "),
    ("'s/d'", " "), ("s/d", " "), ("'sd'", " "),
    ("'golden lion'", " "), ("golden lion", " "), ("goldenlion", " "),
    ("'gl'", " "),
    ("a-spec", " "), ("aspec", " "),
    ("สิงห์ทอง", " "),
    ("เซ็นได", " "),
    ("ตราสิงห์", " "),
]
# Common finish/color suffixes that distinguish variants of the same code
FINISH_TOKENS = ["AC", "NK", "CR", "AB", "SS", "PSS", "BB", "PB", "BN", "AN", "GL"]
FINISH_RE = re.compile(r"\b(" + "|".join(FINISH_TOKENS) + r")\b")
PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")  # trailing parenthetical e.g. (ด้ามดำ)


def normalize_name(s: str) -> str:
    if not s:
        return ""
    t = s.strip()
    # Normalize inch first (uses raw quotes)
    t = INCH_RE.sub(lambda m: m.group(1) + "นิ้ว", t)
    # Strip surrounding quote characters
    for q in QUOTE_CHARS:
        t = t.replace(q, " ")
    t_low = t.lower()
    # Strip brand markers
    for src, dst in BRAND_REPLACEMENTS:
        t_low = t_low.replace(src, dst)
    # Collapse whitespace
    t_low = re.sub(r"\s+", " ", t_low).strip()
    return t_low


def extract_code(s: str):
    if not s:
        return None
    m = CODE_RE.search(s)
    if not m:
        return None
    code = m.group(1).strip().upper()
    return code if len(code) >= 2 else None


def extract_finish(s: str):
    """Pull finish/color suffix (AC/NK/CR/AB/SS) out of name."""
    if not s:
        return None
    # Look at uppercase tokens after the code
    m = FINISH_RE.search(s.upper())
    return m.group(1) if m else None


def tokens_for(s: str):
    n = normalize_name(s)
    raw = re.split(r"[\s/()#'\"\-,.]+", n)
    out = []
    for w in raw:
        w = w.strip()
        if not w or len(w) < 2:
            continue
        out.append(w)
    return set(out)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_xls_rows():
    wb = xlrd.open_workbook(XLS_PATH)
    rows = []
    seen = set()
    for sn in wb.sheet_names():
        sh = wb.sheet_by_name(sn)
        for r in range(1, sh.nrows):
            barcode_raw = sh.cell_value(r, 2)
            name = (sh.cell_value(r, 3) or "").strip()
            brand = (sh.cell_value(r, 4) or "").strip()
            if not name:
                continue
            if isinstance(barcode_raw, float):
                barcode = str(int(barcode_raw))
            else:
                barcode = str(barcode_raw).strip()
            if not barcode or not barcode.isdigit():
                continue
            key = (barcode, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "barcode": barcode,
                "xls_name": name,
                "xls_brand": brand,
                "sheet": sn,
                "row": r + 1,
            })
    return rows


def load_products():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, product_name FROM products WHERE is_active=1"
    ).fetchall()
    conn.close()
    products = []
    for r in rows:
        name = r["product_name"]
        products.append({
            "id":       r["id"],
            "name":     name,
            "norm":     normalize_name(name),
            "code":     extract_code(name),
            "finish":   extract_finish(name),
            "tokens":   tokens_for(name),
        })
    return products


def build_indexes(products):
    by_norm        = defaultdict(list)
    by_code        = defaultdict(list)
    by_code_finish = defaultdict(list)
    for p in products:
        by_norm[p["norm"]].append(p)
        if p["code"]:
            by_code[p["code"]].append(p)
            key = (p["code"], p["finish"] or "")
            by_code_finish[key].append(p)
    return by_norm, by_code, by_code_finish


def match_one(xls_row, products, by_norm, by_code, by_code_finish):
    name   = xls_row["xls_name"]
    norm   = normalize_name(name)
    code   = extract_code(name)
    finish = extract_finish(name)
    xls_tokens = tokens_for(name)

    # 1) Exact normalized name (unique)
    if norm in by_norm and len(by_norm[norm]) == 1:
        return ("matched", by_norm[norm][0], "exact_name", 1.0)

    # 2) Exact (code, finish) tuple — strongest signal for variant products
    if code:
        key = (code, finish or "")
        if key in by_code_finish and len(by_code_finish[key]) == 1:
            return ("matched", by_code_finish[key][0], "code+finish", 1.0)

    # 3) Code unique → match (but if both have a finish and they differ, demote)
    if code and code in by_code and len(by_code[code]) == 1:
        p = by_code[code][0]
        if finish and p["finish"] and finish != p["finish"]:
            return ("review", p, "code_match_finish_diff", 0.7)
        return ("matched", p, "exact_code", 0.95)

    # 4) Code matches multiple — pick best by token jaccard within that pool
    if code and code in by_code and len(by_code[code]) > 1:
        best, best_s = None, 0.0
        for p in by_code[code]:
            # Heavy bonus if finish matches
            s = jaccard(xls_tokens, p["tokens"])
            if finish and p["finish"] == finish:
                s += 0.3
            if s > best_s:
                best, best_s = p, s
        if best and best_s >= 0.65:
            return ("matched", best, "code+best_token", round(min(best_s, 1.0), 3))
        if best and best_s >= 0.40:
            return ("review", best, "code+token_review", round(best_s, 3))

    # 5) No code: fuzzy across all products
    if not code:
        best, best_s = None, 0.0
        for p in products:
            if p["code"]:
                continue  # skip products that have a code (different variant family)
            s = jaccard(xls_tokens, p["tokens"])
            if s > best_s:
                best, best_s = p, s
        if best and best_s >= 0.80:
            return ("matched", best, "fuzzy_no_code", round(best_s, 3))
        if best and best_s >= 0.55:
            return ("review", best, "fuzzy_review", round(best_s, 3))

    return ("unmatched", None, "none", 0.0)


def main():
    if not os.path.exists(XLS_PATH):
        print(f"ERROR: {XLS_PATH} not found (mount external drive?)")
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Reading xls...")
    xls_rows = load_xls_rows()
    print(f"  {len(xls_rows)} unique non-empty rows")

    print("Reading products...")
    products = load_products()
    print(f"  {len(products)} active products")
    by_norm, by_code, by_code_finish = build_indexes(products)

    matched, review, unmatched = [], [], []
    barcode_to_products = defaultdict(list)

    for x in xls_rows:
        status, p, reason, score = match_one(x, products, by_norm, by_code, by_code_finish)
        rec = {
            "barcode":     x["barcode"],
            "xls_name":    x["xls_name"],
            "xls_brand":   x["xls_brand"],
            "match_reason": reason,
            "score":       round(score, 3),
            "product_id":  p["id"]   if p else "",
            "product_name": p["name"] if p else "",
        }
        if status == "matched":
            matched.append(rec)
            barcode_to_products[x["barcode"]].append(p["id"])
        elif status == "review":
            review.append(rec)
        else:
            unmatched.append(rec)

    # Detect ambiguity: same barcode → multiple distinct products (across xls rows)
    ambiguous_barcodes = {b for b, pids in barcode_to_products.items()
                          if len(set(pids)) > 1}

    def write_csv(path, rows, headers):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    headers = ["barcode", "xls_name", "xls_brand", "match_reason", "score",
               "product_id", "product_name"]
    write_csv(os.path.join(OUT_DIR, "barcode_mapping_matched.csv"), matched, headers)
    write_csv(os.path.join(OUT_DIR, "barcode_mapping_review.csv"),  review,  headers)
    write_csv(os.path.join(OUT_DIR, "barcode_mapping_unmatched.csv"), unmatched, headers)

    print()
    print(f"matched   : {len(matched)}")
    print(f"review    : {len(review)}")
    print(f"unmatched : {len(unmatched)}")
    print(f"ambiguous barcodes (same barcode, different products in matched): {len(ambiguous_barcodes)}")
    print()
    print(f"output dir: {OUT_DIR}")


if __name__ == "__main__":
    main()
