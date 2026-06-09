"""Parse each product_name into naming-rule columns and emit a CSV.

Output columns follow the product_name naming rule (sendy_erp/docs/product_name_naming_rule.md):

    sku, product_name (current), category (ประเภท), series (ซีรีส์),
    brand, model, size, color_th, color_code, packaging,
    condition, pack_variant, proposed_name

Parser is heuristic — empty cells mean "couldn't extract automatically".
User reviews + fills missing pieces, then we apply renames.

CLI:
    python sendy_erp/scripts/parse_sku_names.py
    python sendy_erp/scripts/parse_sku_names.py --output /tmp/x.csv
    python sendy_erp/scripts/parse_sku_names.py --only-active
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
DEFAULT_OUT = ROOT / "data" / "exports" / "sku_name_parsed.csv"


CONDITION_TOKENS = (
    "เก่า", "ไม่สวย", "ตำหนิ",
    "หมดอายุ", "ไม่สกรีน", "ไม่มีน็อต",
)
PACKAGING_TOKENS = ("แผง", "ตัว", "ถุง", "แพ็คหัว", "แพ็คถุง",
                    "ซอง", "อัดแผง", "แพ็ค", "แบบหลอด", "โหล")

# Packaging-with-count patterns: "(ซอง100ตัว)" → packaging="ซอง", units_per_box=100
# Tokens that can appear with a count + inner unit
_PKG_COUNT_TOKENS = ("ซอง", "แพ็ค", "โหล", "กล", "กล่อง", "ห่อ", "ถุง")
_PKG_COUNT_INNER_UNITS = ("ตัว", "ดอก", "ใบ", "ชิ้น", "อัน", "เส้น")

ALIASES = {
    "S/D":      "Sendai",
    "เซ็นได":   "Sendai",
    "สิงห์ทอง": "Golden Lion",
    "สิงห์":    "Golden Lion",
    "จระเข้":   "TOA",
}

# Annotations that should be stripped from product names entirely.
ANNOTATIONS = [
    r"\(\s*มีบาโค[๊้]ต\s*\)",
    r"\(\s*ไม่มีบาโค[๊้]ต\s*\)",
    r"\(\s*no\s*barcode\s*\)",
]

# Bare-color Thai/English words → canonical Thai display.
# Detection requires the word to be preceded by 'สี' OR appear as a
# whitespace-bounded token at end-of-name (avoids matching 'หมวกแดง' etc).
BARE_COLORS = {
    "ดำ":     "สีดำ",
    "ขาว":    "สีขาว",
    "แดง":    "สีแดง",
    "น้ำเงิน": "สีน้ำเงิน",
    "เขียว":  "สีเขียว",
    "เหลือง": "สีเหลือง",
    "ส้ม":    "สีส้ม",
    "ม่วง":   "สีม่วง",
    "ชมพู":   "สีชมพู",
    "น้ำตาล": "สีน้ำตาล",
    "ทอง":    "สีทอง",
    "เงิน":   "สีเงิน",
    "ฟ้า":    "สีฟ้า",
    "ชา":     "สีชา",
    "ครีม":   "สีครีม",
    "เทา":    "สีเทา",
    "WHITE":  "สีขาว",
    "BLACK":  "สีดำ",
    "RED":    "สีแดง",
    "BLUE":   "สีน้ำเงิน",
    "GREEN":  "สีเขียว",
    "YELLOW": "สีเหลือง",
    "BROWN":  "สีน้ำตาล",
    "NATURE": "สีธรรมชาติ",
    "งา":      "สีงา",
    "ธรรมชาติ": "สีธรรมชาติ",
}

# Thai aliases for color codes — for bare Thai words that map to a structured
# color_code in `color_finish_codes`. Detected BEFORE bare-color matching so
# they emit (color_code, color_th) instead of just color_th.
#   e.g. 'บรอนซ์' / 'บรอน' → BZ / สีบรอนซ์
COLOR_CODE_ALIASES = {
    "BZ": ["บรอนซ์", "บรอน"],
}

# Known English-uppercase series tokens — detected after brand+model+color
# stripping; placed in the 'series' column.
KNOWN_SERIES = {
    "DOME", "TOP", "HEAVY", "MAX", "PRO", "MINI", "PLUS", "BALL",
    "DEAD LOCK", "NEW TOP",
}


def parse_name(name: str, brand_rec: dict | None,
               color_codes: dict, all_brand_tokens: list,
               token_to_brand: dict | None = None) -> dict:
    out = dict.fromkeys([
        "category", "series", "brand", "model", "size",
        "color_th", "color_code", "packaging", "condition",
        "pack_variant",
    ], "")
    # Optional metadata returned alongside structured fields
    out["units_per_box"] = ""

    if brand_rec:
        out["brand"] = brand_rec.get("name") or ""

    work = name

    # 0a) Strip annotations like "(มีบาโค้ต)" — pure metadata, not part of name.
    for ann in ANNOTATIONS:
        work = re.sub(ann, "", work)

    # 0b) "รุ่น" prefix on packaging or other markers — strip 'รุ่น' globally.
    #   "(รุ่นแผง)"  → "(แผง)"
    #   "รุ่นแผง"   → "(แผง)"  (bare; promote to bracket form)
    #   "รุ่นTOP"   → "TOP"    (series — strip prefix, leave token)
    work = re.sub(r"\(\s*รุ่น\s*", "(", work)
    # bare 'รุ่นX' → '(X)' for packaging tokens
    for tok in PACKAGING_TOKENS:
        work = re.sub(rf"\bรุ่น\s*{tok}\b", f"({tok})", work)
    work = re.sub(r"\bรุ่น\s*", "", work)

    # 0c) Normalize space around '#' model marker.
    #   '# HL316'      → '#HL316'    (no space immediately after #)
    #   '#HL 9991-2'   → '#HL9991-2' (no space between letters and digits)
    work = re.sub(r"#\s+", "#", work)
    work = re.sub(r"#([A-Za-z]+)\s+(\d)", r"#\1\2", work)

    # 0d) Bare model code with leading letters → '#'-prefix it.
    #   'SD9951' (alone)  → '#SD9951'   (model)
    #   'HL316'           → '#HL316'
    # Pattern '2-5 letters + 3-5 digits' is specific enough that brand-collision
    # (SD/GL) is acceptable: 'SD9951' is a model even though SD is also a brand
    # short_code — the digit suffix disambiguates.
    if "#" not in work:
        m_bare = re.search(r"\b([A-Z]{2,5})(\d{3,5})(-\d+)?\b", work)
        if m_bare:
            work = work[:m_bare.start()] + "#" + m_bare.group(0) + work[m_bare.end():]

    # 1) Condition — last bracket per rule 12. Allow optional closing paren
    #    so '(ไม่มีน็อต' (broken bracket) still parses.
    for tok in sorted(CONDITION_TOKENS, key=len, reverse=True):
        m = re.search(rf"\(\s*{re.escape(tok)}\s*\)?", work)
        if m:
            out["condition"] = tok
            work = work[:m.start()] + work[m.end():]
            break

    # 2a) Packaging-with-count: "(ซอง100ตัว)" → packaging=ซอง, units_per_box=100
    #     Tries before the simpler "(ซอง)" match because it's more specific.
    pkg_count_re = (
        r"\(\s*(" + "|".join(_PKG_COUNT_TOKENS) + r")\s*"
        r"(\d+)\s*"
        r"(" + "|".join(_PKG_COUNT_INNER_UNITS) + r")?\s*\)"
    )
    m = re.search(pkg_count_re, work)
    if m:
        out["packaging"] = m.group(1)
        out["units_per_box"] = m.group(2)
        work = work[:m.start()] + work[m.end():]

    # 2b) Packaging (แผง/ตัว/ถุง/...) — second-to-last bracket, simple form
    if not out["packaging"]:
        for tok in PACKAGING_TOKENS:
            m = re.search(rf"\(\s*{tok}\s*\)", work)
            if m:
                out["packaging"] = tok
                work = work[:m.start()] + work[m.end():]
                break

    # 3) Pack-variant — trailing standalone digit (rule 13 legacy)
    m = re.search(r"\s+-?\s*(\d{1,2})\s*$", work)
    if m and int(m.group(1)) <= 9:
        out["pack_variant"] = m.group(1)
        work = work[:m.start()].rstrip(" -")

    # 4) Color code — prefer code-in-parens, then bare token
    for code, name_th in color_codes.items():
        if f"({code})" in work:
            out["color_code"] = code
            out["color_th"] = name_th
            work = re.sub(rf"\s*สี\S+\s*\({code}\)", "", work)
            work = work.replace(f"({code})", "")
            break
    else:
        for code, name_th in color_codes.items():
            if re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", work):
                out["color_code"] = code
                out["color_th"] = name_th
                work = re.sub(rf"(?<![A-Za-z]){code}(?![A-Za-z])", "", work)
                break

    # 4a-bis) Thai aliases for color codes — e.g. 'บรอนซ์'/'บรอน' → BZ.
    #         Only runs if no color_code was matched above.
    if not out["color_code"]:
        for code, aliases in COLOR_CODE_ALIASES.items():
            if code not in color_codes:
                continue  # alias targets unknown code — skip
            for alias in aliases:
                if re.search(rf"{re.escape(alias)}", work):
                    out["color_code"] = code
                    out["color_th"] = color_codes[code]
                    work = re.sub(rf"{re.escape(alias)}", "", work)
                    break
            if out["color_code"]:
                break

    # 4b) After color extraction, re-check for trailing pack-variant marker
    #     (e.g. 'SD9951 - 2 AC' → after AC stripped, '- 2' is leftover suffix).
    if not out["pack_variant"]:
        m = re.search(r"\s+-?\s*(\d{1,2})\s*$", work)
        if m and int(m.group(1)) <= 9:
            out["pack_variant"] = m.group(1)
            work = work[:m.start()].rstrip(" -")

    # 5) Bare colors — only if no color_code already detected.
    #    Boundary chars are STRICT (space/quote/start/end/punct only) so that
    #    'ทอง' inside 'ใบโพธิ์ทอง' (series name) is NOT matched.
    if not out["color_th"]:
        BOUND_BEFORE = r"(?:^|(?<=[\s'\"()\[\].,;:/-]))"
        BOUND_AFTER  = r"(?:$|(?=[\s'\"()\[\].,;:/-]))"
        for word, name_th in BARE_COLORS.items():
            # explicit 'สี<word>' prefix
            patt_prefixed = rf"สี\s*{re.escape(word)}{BOUND_AFTER}"
            if re.search(patt_prefixed, work, flags=re.IGNORECASE):
                out["color_th"] = name_th
                work = re.sub(patt_prefixed, "", work, flags=re.IGNORECASE)
                break
            # bare token (case-insensitive for English colors)
            patt_bare = rf"{BOUND_BEFORE}'?{re.escape(word)}'?{BOUND_AFTER}"
            if re.search(patt_bare, work, flags=re.IGNORECASE):
                out["color_th"] = name_th
                work = re.sub(patt_bare, "", work, flags=re.IGNORECASE)
                break

    # 5z) Reverse-fill color_code from color_th if a basic-color code now
    #     exists in DB (post-mig 038: BLK/WHT/RED/etc.). Only fill when
    #     color_code is still empty — preserves explicit (CODE) detection from step 4.
    if out["color_th"] and not out["color_code"]:
        # Build name_th → code map (case-sensitive Thai)
        name_to_code = {v: k for k, v in color_codes.items()}
        if out["color_th"] in name_to_code:
            out["color_code"] = name_to_code[out["color_th"]]

    # 6) Size — digit (+ optional .frac) + unit. Supports multi-segment:
    #      '4นิ้วx3นิ้วx2.5mm' → '4inx3inx2.5mm'
    #      '120 mm.' → '120mm'
    #      '1.1/2นิ้ว' → '1.5in' (fraction-after-decimal hybrid notation)
    UNIT_GROUP = r"(?:นิ้ว|in\b|cm|CM|mm|MM|มิล)"
    SIZE_SEG = rf"(?:M)?\d+(?:\.\d+)?(?:/\d+)?\s*\.?\s*{UNIT_GROUP}\.?"
    size_re = re.compile(rf"{SIZE_SEG}(?:\s*[x×]\s*{SIZE_SEG})*")
    m = size_re.search(work)
    if m:
        token = m.group(0)
        token = re.sub(r"\s+", "", token)         # collapse whitespace
        token = token.replace("นิ้ว", "in")        # rule 7: นิ้ว → in
        token = token.replace("CM", "cm")          # rule 15: cm lowercase
        token = token.replace("MM", "mm")          # mm lowercase
        token = token.replace("มิล", "mm")         # มิล → mm
        token = token.rstrip(".")                  # drop trailing dot
        # Fraction hybrid 1.1/2in → 1.5in (and similar 2.1/4 → 2.25)
        def frac_to_decimal(mm: re.Match) -> str:
            whole = int(mm.group(1))
            frac_key = mm.group(2)
            FRAC_MAP = {
                "1/2": 0.5,  "1/4": 0.25, "3/4": 0.75,
                "1/8": 0.125,"3/8": 0.375,"5/8": 0.625,"7/8": 0.875,
                "1/3": 0.33, "2/3": 0.67,
            }
            if frac_key in FRAC_MAP:
                return f"{whole + FRAC_MAP[frac_key]:g}"
            return mm.group(0)
        token = re.sub(r"(\d+)\.(\d/\d)", frac_to_decimal, token)
        out["size"] = token
        work = work[:m.start()] + work[m.end():]

    # 7) Model — first '#' token; strip trailing junk like '-.' or '-'
    m = re.search(r"#\S+", work)
    if m:
        token = re.sub(r"[-.,;:]+$", "", m.group(0))
        out["model"] = token
        work = work[:m.start()] + work[m.end():]

    # 7b) Bare-size fallback — 'รีเวท 4-4' / 'รีเวท 4-2' (rivet sizes have
    #     no unit). Run AFTER model so '#XXX' won't be captured here.
    if not out["size"]:
        m = re.search(r"(?<![\w-])(\d{1,2}-\d{1,2})(?![\w-])", work)
        if m:
            out["size"] = m.group(1)
            work = work[:m.start()] + work[m.end():]

    # 8) Strip brand mentions — try bare, quoted, and parenthesized forms.
    #    Parenthesized form handles '(นก)' style brands.
    #    If brand_rec is None but a brand token matches in name, populate
    #    out['brand'] with the matched canonical name.
    brand_token_to_name = {}
    if brand_rec:
        for k in ("name", "name_th", "short_code"):
            v = brand_rec.get(k)
            if v:
                brand_token_to_name[v] = brand_rec["name"]
    # Build map for unowned-by-this-SKU brands too (for fallback population)
    candidates = list(all_brand_tokens)
    candidates += list(ALIASES.keys())
    if brand_rec:
        for k in ("name", "name_th", "short_code"):
            v = brand_rec.get(k)
            if v:
                candidates.append(v)
    candidates.sort(key=lambda s: -len(s))

    for tok in candidates:
        if not tok:
            continue
        for variant in (tok, f"'{tok}'", f"\"{tok}\"", f"({tok})"):
            if variant.lower() in work.lower():
                if not out["brand"]:
                    out["brand"] = (
                        ALIASES.get(tok)
                        or brand_token_to_name.get(tok)
                        or (token_to_brand or {}).get(tok)
                        or tok
                    )
                work = re.sub(re.escape(variant), "", work, flags=re.IGNORECASE)

    # 9) Detect known series tokens (DOME, TOP, etc.)
    detected_series = []
    for tok in sorted(KNOWN_SERIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(tok)}(?!\w)", work):
            detected_series.append(tok)
            work = re.sub(rf"(?<!\w){re.escape(tok)}(?!\w)", "", work)

    # 10) Cleanup leftover for series — strip quotes, '.สี...' patterns,
    #     bare 'สี' token, parens, dots, and extra punctuation.
    work = re.sub(r"['\"()]", "", work)            # parens/quotes
    work = re.sub(r"\.สี\S*", "", work)
    work = re.sub(r"(?<!\w)สี(?!\w)", "", work)
    work = re.sub(r"\s+", " ", work).strip(" -:.,;")

    # 11) Split leftover → category + series
    series_parts = list(detected_series)
    if work:
        parts = work.split(" ", 1)
        out["category"] = parts[0]
        if len(parts) > 1:
            series_parts.append(parts[1].strip())

    if series_parts:
        out["series"] = " ".join(s for s in series_parts if s)

    return out


def build_proposed_name(p: dict) -> str:
    """Reconstruct name following the canonical rule. Used for proposed_name."""
    cat = p["category"] or ""
    ser = p["series"] or ""
    # If series starts with an ASCII letter (e.g. 'DOME', 'TOP'), separate with
    # a space. Thai series like 'พฤกษา' / 'จตุคาม' glue directly to category.
    if cat and ser:
        if re.match(r"^[A-Za-z]", ser):
            head = f"{cat} {ser}"
        else:
            head = cat + ser
    else:
        head = cat or ser
    parts = []
    if head:
        parts.append(head.strip())
    if p["brand"]:
        parts.append(p["brand"])
    if p["model"]:
        # model + size joined with '-'
        if p["size"]:
            parts.append(f"{p['model']}-{p['size']}")
        else:
            parts.append(p["model"])
    elif p["size"]:
        parts.append(p["size"])
    if p["color_th"] and p["color_code"]:
        parts.append(f"{p['color_th']} ({p['color_code']})")
    elif p["color_code"]:
        parts.append(f"({p['color_code']})")
    elif p["color_th"]:
        parts.append(p["color_th"])

    body = " ".join(parts).strip()
    if p["packaging"]:
        body += f" ({p['packaging']})"
    if p["condition"]:
        body += f" ({p['condition']})"
    return body.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--only-active", action="store_true", default=True)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    color_rows = conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY length(code) DESC"
    ).fetchall()
    color_codes = {r["code"]: r["name_th"] for r in color_rows}

    brand_rows = conn.execute(
        "SELECT id, code, name, name_th, short_code FROM brands"
    ).fetchall()
    brands_by_id = {r["id"]: dict(r) for r in brand_rows}
    all_brand_tokens = []
    token_to_brand: dict = {}
    for r in brand_rows:
        for k in ("name", "name_th", "short_code"):
            v = r[k]
            if v:
                all_brand_tokens.append(v)
                token_to_brand[v] = r["name"]

    # products.sku was dropped (mig 097); the parse worklist's "sku" column
    # carries the OLD integer sku (via the forensic legacy map) so the
    # downstream apply_* consumers still resolve it back to a product_id.
    sql = ("SELECT p.id, m.sku, p.product_name, p.brand_id "
           "FROM products p "
           "LEFT JOIN legacy_product_sku_map m ON m.product_id = p.id")
    if not args.all:
        sql += " WHERE p.is_active = 1"
    sql += " ORDER BY p.id"
    rows = conn.execute(sql).fetchall()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "sku", "product_name",
            "category", "series",
            "brand", "model", "size",
            "color_th", "color_code",
            "packaging", "condition", "pack_variant",
            "proposed_name",
        ])
        w.writeheader()
        for r in rows:
            brand_rec = brands_by_id.get(r["brand_id"]) if r["brand_id"] else None
            parts = parse_name(r["product_name"], brand_rec,
                               color_codes, all_brand_tokens,
                               token_to_brand)
            parts["sku"] = r["sku"]
            parts["product_name"] = r["product_name"]
            parts["proposed_name"] = build_proposed_name(parts)
            w.writerow(parts)

    print(f"Parsed {len(rows)} products → {args.output}")


if __name__ == "__main__":
    main()
