"""Product naming + SKU audit (Phase 1 of the product-naming-audit project).

Read-only. Checks the full catalog (2,001 products, including 44 inactive)
against the two locked naming rules and cross-references structured fields.
Emits four report files for Put's Phase 2 review — this script makes NO
database writes (every connection opens `mode=ro`).

See `sendy_erp/docs/product_name_naming_rule.md` (24 rules) and
`sendy_erp/docs/sku_code_naming_rule.md` (10-slot SKU rule) for the rules
themselves, and `projects/product-naming-audit/plan.md` for the full spec.

Three check groups:
  (a) mechanical name-rule violations — tier A, pure textual rewrites with a
      single deterministic target (unit notation, `#` prefix, packaging
      legacy tokens, curated typos, ...). Reused/extended from
      `audit_sku_naming.py` + `autofix_sku_naming.py` (no fork — PR #82 lesson).
  (b) name <-> structured-field mismatch — tier B, judgment required. Per
      Sendai discipline (`.claude/rules/coding-simplicity-ladder.md` +
      verification-discipline), brand/size/color changes are NEVER tier A
      even when a dictionary makes the "correct" value look obvious.
      ~42% of names in this DB intentionally diverge from a full
      rebuild-from-columns (`naming_cascade.py` docstring) — so these checks
      compare ONE field at a time against its literal presence in the name,
      never a full-name-vs-build() diff.
  (c) SKU drift — `build_sku_code()` (the locked generator) vs the stored
      `sku_code`, previewing the D7 collision policy (active wins the bare
      code; same status -> lower product_id wins).

CLI:
    python audit_product_naming.py
    python audit_product_naming.py --db /path/to/inventory.db --output-dir /path/to/out
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_APP_DIR = _SCRIPTS_DIR.parent / "inventory_app"
for _p in (_SCRIPTS_DIR, _APP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import parse_sku_names as psn          # noqa: E402  (scripts/, sibling)
from audit_sku_naming import (         # noqa: E402  (scripts/, prior art — extend, don't fork)
    name_has_brand, name_has_any_brand_token, BRAND_ALIASES,
)
from sku_code_utils import build_sku_code  # noqa: E402  (inventory_app/)
from bsn_suggest import (              # noqa: E402  (inventory_app/)
    _parse_bsn_name, _load_parser_context, _tokenize, _jaccard,
)

DEFAULT_DB = Path.home() / "Sendai-Boonsawat" / "sendy_erp" / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT_DIR = Path.home() / "Sendai-Boonsawat" / "Operations" / "05_analysis-reports" / "data-quality" / "product-naming"


# ---------------------------------------------------------------------------
# Tier A: mechanical, single-target text fixes (name -> name)
# ---------------------------------------------------------------------------

_FRAC_MAP = {
    "1/2": 0.5, "1/4": 0.25, "3/4": 0.75,
    "1/8": 0.125, "3/8": 0.375, "5/8": 0.625, "7/8": 0.875,
    "1/3": 0.33, "2/3": 0.67,
}

# Any inch notation -> canonical `in` (D6 supersedes the rule doc's stale
# `นิ้ว` examples; rules 7+8). Matches '3"', '3 นิ้ว', '3นิ้ว', '1 1/2นิ้ว',
# '1.1/2นิ้ว' (space or dot before the fraction, both accepted per rule 8).
_INCH_RE = re.compile(r'(\d+)(?:[ .](\d/\d))?\s*(?:"|”|″|นิ้ว)\.?')


def fix_inch_to_in(name: str) -> str:
    def repl(m: re.Match) -> str:
        whole, frac = m.group(1), m.group(2)
        if frac and frac in _FRAC_MAP:
            val = int(whole) + _FRAC_MAP[frac]
            return f"{val:g}in"
        if frac:
            return f"{whole} {frac}in"
        return f"{whole}in"
    return _INCH_RE.sub(repl, name)


# mm/cm formatting (rule 15): no space, lowercase, 'มิล' -> 'mm', drop trailing dot.
_MM_CM_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(mm|MM|Mm|mM|cm|CM|Cm|cM|มิล)\.?')


def fix_mm_cm_format(name: str) -> str:
    def repl(m: re.Match) -> str:
        num, unit = m.group(1), m.group(2)
        u = "mm" if (unit == "มิล" or unit.lower() == "mm") else "cm"
        return f"{num}{u}"
    return _MM_CM_RE.sub(repl, name)


# Bare model code (letters+digits, no '#') -> auto-prefix (rule 17). Same
# pattern as parse_sku_names.py step 0d — only runs when no '#' exists
# anywhere in the name yet (avoids double-prefixing an unrelated token).
_BARE_MODEL_RE = re.compile(r'\b([A-Z]{2,5})(\d{3,5})(-\d+)?\b')


def fix_hash_prefix(name: str) -> str:
    if "#" in name:
        return name
    m = _BARE_MODEL_RE.search(name)
    if not m:
        return name
    return name[:m.start()] + "#" + m.group(0) + name[m.end():]


def fix_hash_space(name: str) -> str:
    """Remove spaces inside '# CODE' (rule 18)."""
    out = re.sub(r"#\s+", "#", name)
    out = re.sub(r"#([A-Za-z]+)\s+(\d)", r"#\1\2", out)
    return out


def fix_packaging_legacy(name: str) -> str:
    """'(P)'/'(p)' -> '(แผง)' (rule 10/14)."""
    return re.sub(r"\(\s*[Pp]\s*\)", "(แผง)", name)


def fix_run_prefix(name: str) -> str:
    """Strip 'รุ่น' prefix (rule 14): parenthesized -> keep bracket, bare
    packaging token -> promote to bracket form, bare series token -> strip."""
    out = re.sub(r"\(\s*รุ่น\s*", "(", name)
    for tok in psn.PACKAGING_TOKENS:
        out = re.sub(rf"\bรุ่น\s*{tok}\b", f"({tok})", out)
    out = re.sub(r"\bรุ่น\s*", "", out)
    return out


def fix_annotation_strip(name: str) -> str:
    """Strip pure-metadata annotations like '(มีบาโค้ต)' (rule 16)."""
    out = name
    for ann in psn.ANNOTATIONS:
        out = re.sub(ann, "", out)
    return re.sub(r"\s+", " ", out).strip()


_CONDITION_TOKENS = ("เก่า", "ไม่สวย", "ตำหนิ", "หมดอายุ", "ไม่สกรีน", "ไม่มีน็อต")
_COND_BRACKET_RE = re.compile(
    r"\(\s*(?:" + "|".join(re.escape(t) for t in _CONDITION_TOKENS) + r")\s*\)"
)
_PKG_BRACKET_RE = re.compile(
    r"\(\s*(?:" + "|".join(re.escape(t) for t in psn.PACKAGING_TOKENS) + r")\s*\)"
)


def fix_bracket_order(name: str) -> str:
    """Condition bracket must come AFTER the packaging bracket (rule 12)."""
    cond_m = _COND_BRACKET_RE.search(name)
    pkg_m = _PKG_BRACKET_RE.search(name)
    if not cond_m or not pkg_m or cond_m.start() >= pkg_m.start():
        return name
    cond_text = cond_m.group(0)
    without_cond = name[:cond_m.start()] + name[cond_m.end():]
    without_cond = re.sub(r"\s+", " ", without_cond).strip()
    pkg_m2 = _PKG_BRACKET_RE.search(without_cond)
    if not pkg_m2:
        return name
    result = without_cond[:pkg_m2.end()] + " " + cond_text + without_cond[pkg_m2.end():]
    return re.sub(r"\s+", " ", result).strip()


# Trailing pack-variant digit (rule 13). Requires whitespace directly before
# the digit, which naturally excludes bare rivet sizes like '4-2' (rule 23) —
# there the digit is attached to a dash with no preceding space. Restricted to
# a SINGLE digit 1-3 (the plan's literal spec, and the only values seen in
# real cases) — a wider digit range false-positived on real spec suffixes
# during live-DB validation: 'กาวร้อน TH 18' (product code, not pack-variant)
# and 'คีมคอม้าปากขยับได้ META 8/10/12' (plier sizes with the unit omitted).
_PACK_VARIANT_RE = re.compile(r"\s([1-3])$")


def fix_pack_variant_suffix(name: str) -> str:
    m = _PACK_VARIANT_RE.search(name)
    if not m:
        return name
    return name[:m.start()].rstrip()


def fix_double_space(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


# Curated typo dict (tier A — seeded from the rule doc's "Typos ที่แก้แล้ว"
# table + ปุ๊ก -> พุก per the plan). Append new Put-confirmed typos here.
CURATED_TYPOS = {
    "สแตนแลส":   "สแตนเลส",
    "โครเมี่ยม": "โครเมียม",
    "แสตนเลส":   "สแตนเลส",
    "แสตนแลส":   "สแตนเลส",
    "น๊อต":      "น็อต",
    "เหรีญทอง":  "เหรียญทอง",
    "ปุ๊ก":      "พุก",
}


def fix_typo_curated(name: str) -> str:
    out = name
    for typo, correct in CURATED_TYPOS.items():
        out = out.replace(typo, correct)
    return out


TIER_A_CHECKS = [
    ("INCH_TO_IN",         fix_inch_to_in,         "หน่วยนิ้ว/quote -> in (rule 7/8, D6)"),
    ("MM_CM_FORMAT",       fix_mm_cm_format,       "รูปแบบ mm/cm (rule 15)"),
    ("HASH_PREFIX",        fix_hash_prefix,        "bare model code -> เติม # (rule 17)"),
    ("HASH_SPACE",         fix_hash_space,         "ลบ space ใน # CODE (rule 18)"),
    ("PACKAGING_LEGACY",   fix_packaging_legacy,   "(P) -> (แผง) (rule 10/14)"),
    ("RUN_PREFIX_STRIP",   fix_run_prefix,         "ตัด prefix รุ่น (rule 14)"),
    ("ANNOTATION_STRIP",   fix_annotation_strip,   "ตัด annotation เช่น (มีบาโค้ต) (rule 16)"),
    ("BRACKET_ORDER",      fix_bracket_order,      "จัดลำดับ packaging ก่อน condition (rule 12)"),
    ("PACK_VARIANT_SUFFIX", fix_pack_variant_suffix, "ตัด trailing 1/2/3 (rule 13; ตั้ง units_per_carton แยกต่างหาก)"),
    ("DOUBLE_SPACE",       fix_double_space,       "ยุบ double space (rule 11)"),
    ("TYPO_CURATED",       fix_typo_curated,       "แก้ typo ที่รู้จักแล้ว (curated dict)"),
]


def mechanical_findings(name: str) -> list:
    """Return [(class, proposed_name, note), ...] for each tier-A check whose
    fix actually changes `name`. Each fix is applied independently to the
    ORIGINAL name (not cascaded) so Put can review one class at a time."""
    out = []
    for cls, fn, note in TIER_A_CHECKS:
        proposed = fn(name)
        if proposed != name:
            out.append((cls, proposed, note))
    return out


# ---------------------------------------------------------------------------
# Tier B: name <-> structured-field mismatch (per-field, conservative)
# ---------------------------------------------------------------------------

# Generic catch-all brand buckets (rule doc: "Other (3rd-party) -> ตามชื่อ
# จริงของแบรนด์" / "No Name -> ไม่ต้องเขียนในชื่อ") — by design their OWN name
# never appears in product_name (real DB check: id=13 'Other'/'ทั่วไป' covers
# 62 products with unrelated 3rd-party names), so skip the forward check.
_GENERIC_BRAND_NAMES = {"Other", "No Name"}


def check_brand_mismatch(row: dict, brand_lookup: dict):
    """row: product_name, brand_id. brand_lookup: id -> {name,name_th,short_code}."""
    name = row["product_name"]
    brand_id = row.get("brand_id")
    if brand_id:
        brec = brand_lookup.get(brand_id)
        if brec and brec.get("name") in _GENERIC_BRAND_NAMES:
            return None
        if brec and not name_has_brand(name, brec):
            return f"brand_id={brand_id} ({brec['name']}) ไม่พบ token แบรนด์นี้ใน name"
        return None
    if name_has_any_brand_token(name, brand_lookup):
        for bid, brec in brand_lookup.items():
            if name_has_brand(name, brec):
                return f"brand_id ว่าง แต่ name ดูเหมือนแบรนด์ {brec['name']} (id={bid})"
    return None


_ALNUM_BOUND = r"[A-Za-z0-9]"


def check_color_mismatch(row: dict, color_lookup: dict):
    """row: product_name, color_code. color_lookup: code -> name_th.
    Flags when color_code is set but shows NO support in the name: no
    '(CODE)' bracket, no literal Thai name_th (rule 9's primary form — most
    real names use this, not the bare code), and no bare word-bounded code
    token. Covers pid 1768 (#118AB: 'AB' is embedded in the model number,
    not a standalone token)."""
    name = row["product_name"]
    code = (row.get("color_code") or "").strip()
    if not code:
        return None
    name_th = color_lookup.get(code)
    if f"({code})" in name:
        return None
    if name_th and name_th in name:
        return None
    if re.search(rf"(?<!{_ALNUM_BOUND}){re.escape(code)}(?!{_ALNUM_BOUND})", name):
        return None
    if not name_th:
        return f"color_code={code} (ไม่อยู่ใน color_finish_codes dict)"
    return f"color_code={code} ({name_th}) ไม่พบใน name (ไม่มีวงเล็บ ไม่มี name_th ไม่มี bare code)"


def check_packaging_mismatch(row: dict, parsed: dict):
    pkg_col = (row.get("packaging_th") or "").strip()
    pkg_parsed = (parsed.get("packaging") or "").strip()
    if pkg_col and pkg_parsed and pkg_col != pkg_parsed:
        return f"packaging_th={pkg_col} แต่ name ระบุ ({pkg_parsed})"
    return None


def _norm_model(s: str) -> str:
    return s.lstrip("#").strip().upper()


def check_model_mismatch(row: dict, parsed: dict):
    """Rule 6 joins model+size with '-' and no space (e.g. '#306-0.5kg'), so
    the parser's greedy '#\\S+' model token naturally swallows the trailing
    size too. Compare by prefix (parsed startswith the stored model, followed
    by a non-alnum separator or end-of-string) rather than exact equality —
    real-DB check found exact-equality false-positived on ~all model+size
    products (e.g. stored '#306' vs parsed '#306-0.5kg')."""
    model_col = _norm_model((row.get("model") or "").strip())
    model_parsed = _norm_model((parsed.get("model") or "").strip())
    if not model_col or not model_parsed:
        return None
    if model_parsed == model_col:
        return None
    if model_parsed.startswith(model_col):
        tail = model_parsed[len(model_col):len(model_col) + 1]
        if not tail.isalnum():
            return None
    return f"model={row.get('model')} แต่ name ระบุ {parsed.get('model')}"


def check_series_mismatch(row: dict):
    """Direct substring presence check (not the parser's derived 'series'
    field, which is a leftover-text heuristic too noisy for comparison).
    Also tries the value with '_' -> ' ' — real-DB check found ~200 products
    store series with underscore-joined tokens (e.g. 'NEW_TOP', '2_IN1')
    while the name itself uses spaces ('NEW TOP', '2 IN1'); that's a stored-
    value formatting quirk, not a real name/field conflict."""
    name = row["product_name"]
    series_col = (row.get("series") or "").strip()
    if not series_col:
        return None
    if series_col in name or series_col.replace("_", " ") in name:
        return None
    return f"series={series_col} ไม่พบคำนี้ใน name เลย (เทียบทั้งค่าจริงและแบบแทน _ ด้วยช่องว่าง)"


# ---------------------------------------------------------------------------
# Dictionary-level checks (color_finish_codes / brands) — generic, testable
# ---------------------------------------------------------------------------

def _strip_sii(name_th: str) -> str:
    return name_th[1:] if name_th.startswith("สี") else name_th


def find_color_dict_issues(color_rows) -> dict:
    """color_rows: iterable of (code, name_th).
    duplicate_names: groups of codes sharing an identical name_th (catches
      JSN=NK, both 'สีนิกเกิล').
    combo_conflicts: combo codes (code contains '/') where a slash-split
      segment doesn't match ITS OWN base code's name but exactly matches a
      DIFFERENT code's full name — signals borrowed wording (catches BN/PB
      reading 'นิกเกิล', which belongs to NK/JSN, instead of BN's 'น้ำตาลเข้ม').
      Deliberately narrow (exact-match only) so legitimate shorthand combos
      like SB/PB (which abbreviates the shared 'ทอง' root) are NOT flagged.
    """
    color_rows = list(color_rows)
    stripped = {code: _strip_sii(name) for code, name in color_rows}

    by_name = defaultdict(list)
    for code, name in color_rows:
        by_name[name].append(code)
    duplicate_names = [{"name_th": n, "codes": sorted(cs)}
                        for n, cs in by_name.items() if len(cs) > 1]

    combo_conflicts = []
    for code, name in color_rows:
        if "/" not in code:
            continue
        code_parts = code.split("/")
        name_parts = stripped[code].split("/")
        if len(code_parts) != len(name_parts):
            continue
        for cp, seg in zip(code_parts, name_parts):
            own = stripped.get(cp)
            if own is None or own == seg:
                continue
            other_owners = [c2 for c2, n2 in stripped.items()
                             if n2 == seg and c2 != code]
            if other_owners:
                combo_conflicts.append({
                    "combo_code": code, "combo_name_th": name,
                    "segment_code": cp, "segment_name": seg,
                    "expected_name": own, "borrowed_from": other_owners,
                })
    return {"duplicate_names": duplicate_names, "combo_conflicts": combo_conflicts}


def find_brand_alias_conflicts(brand_rows) -> list:
    """brand_rows: iterable of {id, name, name_th}. Flags any brand whose
    name_th is a KEY in the naming rule's brand-alias table (parse_sku_names
    .ALIASES) but whose own canonical `name` differs from the alias target
    — e.g. Crocodile (name_th='จระเข้') vs the rule's 'จระเข้' -> TOA mapping."""
    out = []
    for rec in brand_rows:
        name_th = rec.get("name_th")
        if not name_th or name_th not in psn.ALIASES:
            continue
        target = psn.ALIASES[name_th]
        if rec.get("name") != target:
            out.append({
                "brand_id": rec["id"], "name": rec.get("name"),
                "name_th": name_th, "alias_target": target,
            })
    return out


# ---------------------------------------------------------------------------
# Group (c): SKU drift + D7 collision preview
# ---------------------------------------------------------------------------

def canonical_sku_for_row(row: dict) -> str:
    return build_sku_code(row)


def resolve_collisions(computed: dict, active_map: dict) -> dict:
    """computed: pid -> canonical base sku. active_map: pid -> is_active (1/0).
    Returns pid -> (final_sku, collision_note) per D7: active wins the bare
    code; same status -> lower product_id wins; losers get '-{id}'."""
    groups = defaultdict(list)
    for pid, base in computed.items():
        groups[base].append(pid)

    result = {}
    for base, pids in groups.items():
        if len(pids) == 1:
            result[pids[0]] = (base, "")
            continue
        ordered = sorted(pids, key=lambda p: (0 if active_map.get(p) else 1, p))
        winner = ordered[0]
        losers = ordered[1:]
        result[winner] = (base, f"collision with pid {losers} — winner (active/lower-id)")
        for p in losers:
            result[p] = (f"{base}-{p}", f"collision with winner pid {winner} — disambiguated")
    return result


# ---------------------------------------------------------------------------
# Best-effort merge-target lookup for inactive products
# ---------------------------------------------------------------------------

_MERGED_PREFIX_RE = re.compile(r"^\[MERGED[→>-]+(\d+)\]")

# Sourced from memory project_2026_07_06_orbit_bac_clamp_consolidation.md —
# these 4 were merged same-day (2026-07-06) and predate a name-prefix
# convention, so they carry no '[MERGED→pid]' tombstone in the name itself.
_KNOWN_MERGE_TARGETS = {1111: 569, 1112: 568, 1114: 566, 1116: 565}

_SIMILARITY_THRESHOLD = 0.5


def find_merge_target(pid: int, name: str, active_products: list):
    """Best-effort only. Returns (target_pid_or_None, confidence_note)."""
    m = _MERGED_PREFIX_RE.match(name)
    if m:
        return int(m.group(1)), "explicit [MERGED→pid] prefix in name"
    if pid in _KNOWN_MERGE_TARGETS:
        return _KNOWN_MERGE_TARGETS[pid], "sourced from memory (ORBIT/BAC consolidation, 2026-07-06)"

    tokens = _tokenize(name)
    if not tokens:
        return None, "no confident match found (name has no comparable tokens)"
    best_pid, best_score = None, 0.0
    for ap in active_products:
        score = _jaccard(tokens, _tokenize(ap["product_name"]))
        if score > best_score:
            best_pid, best_score = ap["id"], score
    if best_score >= _SIMILARITY_THRESHOLD:
        return best_pid, f"heuristic name-similarity match (jaccard={best_score:.2f})"
    return None, "no confident match found (best heuristic score below threshold)"


# ---------------------------------------------------------------------------
# Evidence gathering (DB-touching, only for flagged rows)
# ---------------------------------------------------------------------------

def evidence_for_product(conn, product_id: int) -> dict:
    """Mode of BSN raw name (sales+purchase — BSN raw names are spec-accurate;
    marketplace rows are excluded, per verification-discipline) + last sale
    date, for a tier-B judgment row."""
    rows = conn.execute("""
        SELECT product_name_raw, date_iso FROM (
            SELECT product_name_raw, date_iso FROM sales_transactions
             WHERE product_id = ? AND customer NOT LIKE 'หน้าร้าน%'
            UNION ALL
            SELECT product_name_raw, date_iso FROM purchase_transactions
             WHERE product_id = ?
        )
    """, (product_id, product_id)).fetchall()
    names = [r["product_name_raw"] for r in rows if r["product_name_raw"]]
    mode_name = Counter(names).most_common(1)[0][0] if names else ""

    last_sale = conn.execute("""
        SELECT MAX(date_iso) AS d FROM sales_transactions
         WHERE product_id = ? AND customer NOT LIKE 'หน้าร้าน%'
    """, (product_id,)).fetchone()["d"] or ""

    return {"bsn_raw_mode": mode_name, "last_sale": last_sale}


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_audit(db_path: Path):
    conn = _connect_ro(db_path)
    ctx = _load_parser_context(conn)
    brand_lookup = ctx["brands_by_id"]
    color_lookup = ctx["color_codes"]

    products = conn.execute("""
        SELECT p.id, p.product_name, p.is_active, p.brand_id, p.category_id,
               p.color_code, p.model, p.size, p.series, p.packaging_th,
               p.packaging_short, p.condition, p.pack_variant,
               p.sub_category_short_code, p.sku_code, p.sku_code_locked,
               b.short_code AS brand_short_code,
               c.short_code AS cat_short_code
          FROM products p
          LEFT JOIN brands b     ON b.id = p.brand_id
          LEFT JOIN categories c ON c.id = p.category_id
         ORDER BY p.id
    """).fetchall()
    products = [dict(r) for r in products]

    mechanical_rows = []
    judgment_rows = []

    for row in products:
        pid = row["id"]
        name = row["product_name"]

        for cls, proposed, note in mechanical_findings(name):
            mechanical_rows.append({
                "class": cls, "product_id": pid, "is_active": row["is_active"],
                "current_name": name, "proposed_name": proposed, "note": note,
            })

        parsed = _parse_bsn_name(name, ctx)
        field_checks = [
            ("BRAND_MISMATCH", check_brand_mismatch(row, brand_lookup)),
            ("COLOR_MISMATCH", check_color_mismatch(row, color_lookup)),
            ("PACKAGING_MISMATCH", check_packaging_mismatch(row, parsed)),
            ("MODEL_MISMATCH", check_model_mismatch(row, parsed)),
            ("SERIES_MISMATCH", check_series_mismatch(row)),
        ]
        if len(name) > 60:
            field_checks.append(("TOO_LONG", f"ชื่อยาว {len(name)} ตัวอักษร (>60)"))

        for issue, note in field_checks:
            if not note:
                continue
            ev = evidence_for_product(conn, pid)
            judgment_rows.append({
                "product_id": pid, "is_active": row["is_active"], "issue": issue,
                "current_name": name,
                "current_fields": (f"brand_id={row['brand_id']} color_code={row['color_code']} "
                                    f"model={row['model']} size={row['size']} series={row['series']} "
                                    f"packaging_th={row['packaging_th']}"),
                "parsed_from_name": str(parsed),
                "evidence_bsn_raw": ev["bsn_raw_mode"],
                "evidence_last_sale": ev["last_sale"],
                "proposal": note,
                "decision": "",
            })

    # Dictionary-level: color duplicate names + combo conflicts
    color_rows = conn.execute("SELECT code, name_th FROM color_finish_codes").fetchall()
    color_issues = find_color_dict_issues([(r["code"], r["name_th"]) for r in color_rows])
    for grp in color_issues["duplicate_names"]:
        affected = conn.execute(
            f"SELECT id, is_active FROM products WHERE color_code IN ({','.join('?' * len(grp['codes']))})",
            grp["codes"],
        ).fetchall()
        for r in affected:
            pid = r["id"]
            prow = next(p for p in products if p["id"] == pid)
            ev = evidence_for_product(conn, pid)
            judgment_rows.append({
                "product_id": pid, "is_active": r["is_active"],
                "issue": "COLOR_DICT_DUPLICATE",
                "current_name": prow["product_name"],
                "current_fields": f"color_code={prow['color_code']}",
                "parsed_from_name": "",
                "evidence_bsn_raw": ev["bsn_raw_mode"], "evidence_last_sale": ev["last_sale"],
                "proposal": (f"color codes {grp['codes']} ทั้งหมดแปลว่า '{grp['name_th']}' "
                             f"ซ้ำกัน — ตัดสินใจว่าจะรวมเป็นโค้ดเดียวหรือคงแยกไว้"),
                "decision": "",
            })
    for conflict in color_issues["combo_conflicts"]:
        affected = conn.execute(
            "SELECT id, is_active FROM products WHERE color_code = ?",
            (conflict["combo_code"],),
        ).fetchall()
        for r in affected:
            pid = r["id"]
            prow = next(p for p in products if p["id"] == pid)
            ev = evidence_for_product(conn, pid)
            judgment_rows.append({
                "product_id": pid, "is_active": r["is_active"],
                "issue": "COLOR_DICT_CONFLICT",
                "current_name": prow["product_name"],
                "current_fields": f"color_code={conflict['combo_code']}",
                "parsed_from_name": "",
                "evidence_bsn_raw": ev["bsn_raw_mode"], "evidence_last_sale": ev["last_sale"],
                "proposal": (f"{conflict['combo_code']} ('{conflict['combo_name_th']}') ส่วน "
                             f"{conflict['segment_code']} อ่านว่า '{conflict['segment_name']}' "
                             f"ซึ่งเป็นความหมายของ {conflict['borrowed_from']} ไม่ใช่ของตัวเอง "
                             f"('{conflict['expected_name']}') — ตรวจสอบ color_finish_codes"),
                "decision": "",
            })

    # Dictionary-level: brand alias conflicts (จระเข้ TOA vs Crocodile, etc.)
    brand_rows_all = conn.execute("SELECT id, name, name_th FROM brands").fetchall()
    for conflict in find_brand_alias_conflicts([dict(r) for r in brand_rows_all]):
        affected = conn.execute(
            "SELECT id, is_active FROM products WHERE brand_id = ?",
            (conflict["brand_id"],),
        ).fetchall()
        for r in affected:
            pid = r["id"]
            prow = next(p for p in products if p["id"] == pid)
            ev = evidence_for_product(conn, pid)
            judgment_rows.append({
                "product_id": pid, "is_active": r["is_active"],
                "issue": "BRAND_DICT_ALIAS_CONFLICT",
                "current_name": prow["product_name"],
                "current_fields": f"brand_id={conflict['brand_id']} ({conflict['name']})",
                "parsed_from_name": "",
                "evidence_bsn_raw": ev["bsn_raw_mode"], "evidence_last_sale": ev["last_sale"],
                "proposal": (f"brand '{conflict['name']}' (id={conflict['brand_id']}) มี name_th "
                             f"'{conflict['name_th']}' ซ้ำกับ alias rule ที่ map ไป "
                             f"'{conflict['alias_target']}' — ตรวจว่าควร merge brand หรือแก้ alias table"),
                "decision": "",
            })

    # Group (c): SKU drift
    computed, active_map = {}, {}
    for row in products:
        if row["sku_code_locked"]:
            continue
        computed[row["id"]] = canonical_sku_for_row(row)
        active_map[row["id"]] = row["is_active"]
    resolved = resolve_collisions(computed, active_map)

    sku_rows = []
    for row in products:
        pid = row["id"]
        if row["sku_code_locked"]:
            continue
        final_sku, collision_note = resolved[pid]
        stored = row["sku_code"] or ""
        if final_sku != stored:
            sku_rows.append({
                "product_id": pid, "is_active": row["is_active"],
                "locked": row["sku_code_locked"],
                "stored_sku": stored, "canonical_sku": final_sku,
                "collision_note": collision_note,
            })

    # inactive_44: best-effort merge target
    active_products = [r for r in products if r["is_active"]]
    inactive_rows = []
    for row in products:
        if row["is_active"]:
            continue
        target, note = find_merge_target(row["id"], row["product_name"], active_products)
        inactive_rows.append({
            "product_id": row["id"], "name": row["product_name"],
            "merge_target": target, "confidence": note,
        })

    conn.close()
    return {
        "mechanical": mechanical_rows,
        "judgment": judgment_rows,
        "sku_drift": sku_rows,
        "inactive": inactive_rows,
        "total_products": len(products),
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_reports(results: dict, out_dir: Path, tag: str):
    out_dir.mkdir(parents=True, exist_ok=True)

    mech_path = out_dir / f"audit_mechanical_{tag}.csv"
    with open(mech_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["class", "product_id", "is_active",
                                           "current_name", "proposed_name", "note"])
        w.writeheader()
        w.writerows(results["mechanical"])

    judg_path = out_dir / f"audit_judgment_{tag}.csv"
    with open(judg_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "is_active", "issue",
                                           "current_name", "current_fields",
                                           "parsed_from_name", "evidence_bsn_raw",
                                           "evidence_last_sale", "proposal", "decision"])
        w.writeheader()
        w.writerows(results["judgment"])

    sku_path = out_dir / f"audit_sku_drift_{tag}.csv"
    with open(sku_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "is_active", "locked",
                                           "stored_sku", "canonical_sku", "collision_note"])
        w.writeheader()
        w.writerows(results["sku_drift"])

    inactive_path = out_dir / f"inactive_44_{tag}.md"
    with open(inactive_path, "w", encoding="utf-8") as f:
        f.write(f"# Inactive products — best-effort merge targets ({tag})\n\n")
        f.write(f"Total inactive: {len(results['inactive'])}\n\n")
        f.write("| product_id | name | merge_target | confidence |\n")
        f.write("|---|---|---|---|\n")
        for r in results["inactive"]:
            target = r["merge_target"] if r["merge_target"] else "(none)"
            f.write(f"| {r['product_id']} | {r['name']} | {target} | {r['confidence']} |\n")

    return mech_path, judg_path, sku_path, inactive_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    before_mtime = args.db.stat().st_mtime
    results = run_audit(args.db)
    after_mtime = args.db.stat().st_mtime
    if before_mtime != after_mtime:
        sys.exit("REFUSING TO CONTINUE: DB file mtime changed during audit (should be impossible — read-only connection).")

    paths = write_reports(results, args.output_dir, args.date)

    print(f"Total products audited: {results['total_products']}")
    print(f"Tier A (mechanical):    {len(results['mechanical'])} findings")
    class_counts = Counter(r["class"] for r in results["mechanical"])
    for cls, n in class_counts.most_common():
        print(f"  {cls:<22} {n}")
    print(f"Tier B (judgment):      {len(results['judgment'])} findings")
    issue_counts = Counter(r["issue"] for r in results["judgment"])
    for issue, n in issue_counts.most_common():
        print(f"  {issue:<28} {n}")
    print(f"SKU drift:              {len(results['sku_drift'])}")
    print(f"Inactive (best-effort):  {len(results['inactive'])}")
    print()
    for p in paths:
        print(f"Report: {p}")


if __name__ == "__main__":
    main()
