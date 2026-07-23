"""Family-consistency audit (Phase 1 of the product-naming-round2 project).

Read-only, `is_active=1` scope only (D1). Extends round-1's per-row rule
audit (`audit_product_naming.py`) with checks round 1 never covered:
cross-row consistency WITHIN a product family, แผง/ตัว twin alignment (via
shared `bsn_code`), collision detection on the resulting canonical names, and
a targeted anomaly check for generic-brand rows whose name text actually
names a real (non-generic) sibling brand.

See `projects/product-naming-round2/plan.md` for the locked decisions
(D1-D6) and seed findings this detector must reproduce: the ถุงหิ้ว
two-pattern family (pids 683-687 vs 1369-1374), the บานพับผีเสื้อ twin
(pid 96 / pid 1888, shared bsn_code 030บ4000), and the pid 686 brand_id
anomaly.

Four check groups:
  1. Family grouping + structural signature — active products are grouped by
     (category_id, brand_id), then further clustered by a leading-token
     prefix relationship (so e.g. 'ถุงหิ้วคละสี' clusters with 'ถุงหิ้ว').
     Within a cluster, a per-row signature (has #model / has unit-bearing
     size / has bare WxH size / color representation) is compared against
     the cluster's majority signature. Divergent rows are repaired
     mechanically (bare-size -> unit-bearing, reusing the family's own
     already-present tokens) when possible, or routed to the ambiguous list
     when the fix would require ADDING identity info (model/color) the row
     doesn't already carry in its own name (D2 — no hard linkage at the
     family level, only per-code twin linkage counts as hard).
  2. Twin sweep — bsn_codes mapped to >1 distinct active product_id
     (`product_code_mapping`). Names are compared after stripping the
     packaging bracket; misaligned pairs get a spec-inheritance proposal
     (the less-richly-specified row inherits the richer sibling's structure
     plus its own bsn_unit as the packaging suffix) when richness is
     unambiguous, else surfaced with no forced direction.
  3. Collision detection — proposed final names (after 1+2) duplicated
     across active products -> merge/differentiate/park queue (D3).
  4. Generic-brand text anomaly — a row's brand_id points at a generic
     bucket (Other/No Name) but its name literally contains ANOTHER, real
     brand's name_th, corroborated by that brand having active siblings in
     the same category (independent evidence, not just text coincidence).

CLI:
    python audit_family_consistency.py
    python audit_family_consistency.py --db /path/to/inventory.db --output-dir /path/to/out
"""
from __future__ import annotations

import argparse
import csv
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

import parse_sku_names as psn                          # noqa: E402  (scripts/, sibling)
from audit_product_naming import evidence_for_product   # noqa: E402  (reuse, don't fork)

DEFAULT_DB = Path.home() / "Sendai-Boonsawat" / "sendy_erp" / "inventory_app" / "instance" / "inventory.db"
DEFAULT_OUT_DIR = Path.home() / "Sendai-Boonsawat" / "Operations" / "05_analysis-reports" / "data-quality" / "product-naming"

_GENERIC_BRAND_NAMES = {"Other", "No Name"}

# ---------------------------------------------------------------------------
# 1a. Structural signature (per-row)
# ---------------------------------------------------------------------------

_UNIT_SIZE_RE = re.compile(r'\d+(?:\.\d+)?(?:[x×]\d+(?:\.\d+)?)*\s*(?:in|mm|cm|นิ้ว)\b', re.IGNORECASE)
# Bare WxH dimension with NO adjacent unit. The trailing \b naturally excludes
# '...11in' — a digit run directly followed by a unit LETTER has no word
# boundary between them (both \w), so '\d+\b' fails to match right before
# 'in'/'mm'/'cm' and the whole alternation backtracks to no-match.
_BARE_DIM_RE = re.compile(r'\b\d+(?:\.\d+)?(?:[x×]\d+(?:\.\d+)?)+\b')
_HASH_MODEL_RE = re.compile(r'#\S+')
_UNIT_TOKEN_RE = re.compile(r'(in|mm|cm|นิ้ว)\b', re.IGNORECASE)
_COLOR_BRACKET_RE = re.compile(r'\([A-Z]{2,6}(?:/[A-Z]{2,6})?\)')


def has_unit_size(name: str) -> bool:
    return bool(_UNIT_SIZE_RE.search(name))


def has_bare_size(name: str) -> bool:
    return bool(_BARE_DIM_RE.search(name))


def has_hash_model(name: str) -> bool:
    return bool(_HASH_MODEL_RE.search(name))


_BOUND_BEFORE = r"(?:^|(?<=[\s'\"()\[\].,;:/-]))"
_BOUND_AFTER = r"(?:$|(?=[\s'\"()\[\].,;:/-]))"


def color_repr(name: str, color_codes: dict) -> str:
    """'coded' if a known color code appears as '(CODE)', 'bare' if a bare
    Thai/English color word is present (rule 19), else 'none'. Mirrors
    parse_sku_names.py's own two-mode bare-color match (explicit 'สี<word>'
    prefix, or a strictly boundary-wrapped bare token) so a color word glued
    inside an unrelated word (e.g. 'ทอง' in 'ใบโพธิ์ทอง') is NOT matched —
    same false-positive guard as the round-1 parser."""
    for code in color_codes:
        if f"({code})" in name:
            return "coded"
    for word in psn.BARE_COLORS:
        patt_prefixed = rf"สี\s*{re.escape(word)}{_BOUND_AFTER}"
        if re.search(patt_prefixed, name, flags=re.IGNORECASE):
            return "bare"
        patt_bare = rf"{_BOUND_BEFORE}{re.escape(word)}{_BOUND_AFTER}"
        if re.search(patt_bare, name, flags=re.IGNORECASE):
            return "bare"
    return "none"


def structural_signature(name: str, color_codes: dict) -> tuple:
    return (has_hash_model(name), has_unit_size(name), has_bare_size(name),
            color_repr(name, color_codes))


def classify_divergence(row_sig: tuple, majority_sig: tuple) -> str:
    """'size_format' when the divergence is ONLY in the bare/unit-size axes
    (pure reformatting of an already-present value — mechanical, no new
    identity). 'identity_missing' when the row lacks a #model or a color the
    majority pattern has (or vice versa) — fixing that means ADDING
    identity info, which needs Put's judgment absent hard linkage (D2)."""
    has_model_r, _unit_r, _bare_r, color_r = row_sig
    has_model_m, _unit_m, _bare_m, color_m = majority_sig
    if has_model_r == has_model_m and color_r == color_m:
        return "size_format"
    return "identity_missing"


# ---------------------------------------------------------------------------
# 1b. Leading-token clustering (keeps unrelated products sharing only
#     category+brand from being lumped into one false "family")
# ---------------------------------------------------------------------------

def leading_token(name: str) -> str:
    return name.strip().split(" ", 1)[0] if name and name.strip() else ""


def shares_prefix(a: str, b: str, min_prefix: int = 4) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= min_prefix and longer.startswith(shorter)


def cluster_by_leading_token(rows: list, min_prefix: int = 4) -> list:
    """rows: dicts with 'id' + 'product_name'. Union-find over pairs whose
    leading token shares a prefix relationship. Returns list of clusters."""
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    tokens = [leading_token(r["product_name"]) for r in rows]
    for i in range(n):
        for j in range(i + 1, n):
            if shares_prefix(tokens[i], tokens[j], min_prefix):
                union(i, j)

    clusters = defaultdict(list)
    for i, r in enumerate(rows):
        clusters[find(i)].append(r)
    return list(clusters.values())


def _pick_canonical_signature(sig_counts: Counter) -> tuple:
    """Pick the family's canonical signature. Prefers a unit-bearing size
    (has_unit_size=True) over a bare size WHENEVER both exist in the family,
    regardless of raw count — rule 7/8 mandates the unit, so a 3-vs-2 split
    in the WRONG direction must not out-vote the rule. Falls back to plain
    majority for the other axes (#model / color presence), where the naming
    rule doesn't dictate a universal preference."""
    unit_sigs = [s for s in sig_counts if s[1] is True]
    if unit_sigs:
        return max(unit_sigs, key=lambda s: sig_counts[s])
    return sig_counts.most_common(1)[0][0]


def find_divergent_families(products: list, color_codes: dict) -> list:
    """products: active rows with id, product_name, category_id, brand_id.
    Returns list of {category_id, brand_id, rows, signatures, majority_signature}."""
    by_cat_brand = defaultdict(list)
    for r in products:
        by_cat_brand[(r["category_id"], r["brand_id"])].append(r)

    families = []
    for (cat_id, brand_id), rows in by_cat_brand.items():
        if len(rows) < 2:
            continue
        for cluster in cluster_by_leading_token(rows):
            if len(cluster) < 2:
                continue
            sigs = {r["id"]: structural_signature(r["product_name"], color_codes) for r in cluster}
            distinct = set(sigs.values())
            if len(distinct) < 2:
                continue
            majority_sig = _pick_canonical_signature(Counter(sigs.values()))
            families.append({
                "category_id": cat_id, "brand_id": brand_id,
                "rows": cluster, "signatures": sigs,
                "majority_signature": majority_sig,
            })
    return families


def _infer_unit(rows: list) -> str:
    for r in rows:
        m = _UNIT_TOKEN_RE.search(r["product_name"])
        if m:
            u = m.group(1).lower()
            return "in" if u == "นิ้ว" else u
    return "in"


def propose_mechanical_fix(name: str, majority_category_prefix: str,
                            brand_text: str, default_unit: str = "in"):
    """Best-effort textual repair for a bare-dimension divergence: peel any
    extra qualifier glued to the category prefix, append the family's unit
    to the bare WxH dimension, and rebuild as
    '[category] [brand] [size+unit] [extra qualifier]'.
    Returns (proposed_name_or_None, fix_kind)."""
    if has_unit_size(name) or not has_bare_size(name):
        return None, "needs_manual_family_review"
    if brand_text not in name:
        return None, "needs_manual_family_review"
    work = name.replace(brand_text, " ")
    m = _BARE_DIM_RE.search(work)
    if not m:
        return None, "needs_manual_family_review"
    size_token = m.group(0)
    head = (work[:m.start()] + work[m.end():]).strip()
    head = re.sub(r"\s+", " ", head)
    if not head.startswith(majority_category_prefix):
        return None, "needs_manual_family_review"
    extra = head[len(majority_category_prefix):].strip()
    # A genuine trailing qualifier (e.g. 'คละสี') is descriptive text, never
    # digits/slashes. A digit/slash leftover means _BARE_DIM_RE grabbed only
    # PART of a larger spec — e.g. 'STAR 1/4x4' is a '1/4in x 4in' drill-bit
    # fraction spec, and the bare regex matches just '4x4', stranding '1/' —
    # bail rather than emit a mangled name (2026-07-21, caught by full-DB run).
    if re.search(r"[\d/]", extra):
        return None, "needs_manual_family_review"
    parts = [majority_category_prefix, brand_text, f"{size_token}{default_unit}"]
    if extra:
        parts.append(extra)
    return " ".join(parts), "mechanical_auto"


# ---------------------------------------------------------------------------
# 2. Twin sweep (bsn_code shared by >1 active product)
# ---------------------------------------------------------------------------

_PKG_BRACKET_RE = re.compile(
    r"\s*\(\s*(?:" + "|".join(re.escape(t) for t in psn.PACKAGING_TOKENS) + r")\s*\)"
)


def strip_packaging_bracket(name: str) -> str:
    out = _PKG_BRACKET_RE.sub("", name)
    return re.sub(r"\s+", " ", out).strip()


def _size_segment_count(name: str) -> int:
    m = _UNIT_SIZE_RE.search(name)
    if not m:
        return 0
    return len(re.findall(r"\d+(?:\.\d+)?", m.group(0)))


def name_richness(name: str) -> tuple:
    """Comparable richness score, higher = more structured info present:
    (has_model, size_segment_count, has_color_code_bracket). Deliberately
    excludes raw text length — a longer name from extra glued/redundant
    text (e.g. 'บานพับสีทอง' vs 'บานพับ' when '(GP)' already says the same
    thing) is NOT more structurally rich, it's a genuine tie needing Put's
    per-row judgment, not a forced direction."""
    return (
        1 if has_hash_model(name) else 0,
        _size_segment_count(name),
        1 if _COLOR_BRACKET_RE.search(name) else 0,
    )


def find_twins(conn) -> list:
    """Returns list of {bsn_code, entries:[{product_id,product_name,bsn_unit}], aligned}."""
    rows = conn.execute("""
        SELECT m.bsn_code, m.product_id, m.bsn_unit, p.product_name
          FROM product_code_mapping m
          JOIN products p ON p.id = m.product_id
         WHERE m.product_id IS NOT NULL AND p.is_active = 1
    """).fetchall()
    by_code = defaultdict(list)
    for r in rows:
        by_code[r["bsn_code"]].append(dict(r))

    twins = []
    for bsn_code, group in by_code.items():
        by_pid = {}
        for g in group:
            by_pid.setdefault(g["product_id"], g)
        if len(by_pid) < 2:
            continue
        entries = [by_pid[p] for p in sorted(by_pid)]
        stripped = [strip_packaging_bracket(e["product_name"]) for e in entries]
        aligned = len(set(stripped)) == 1
        twins.append({"bsn_code": bsn_code, "entries": entries, "aligned": aligned})
    return twins


def propose_twin_inheritance(entries: list) -> dict:
    """Returns {product_id: proposed_name} for the less-rich entries, or {}
    if the richest entry ties with the runner-up (no confident direction —
    Put decides per-row)."""
    scored = sorted(entries, key=lambda e: name_richness(e["product_name"]), reverse=True)
    richest, rest = scored[0], scored[1:]
    if rest and name_richness(rest[0]["product_name"]) == name_richness(richest["product_name"]):
        return {}
    base = strip_packaging_bracket(richest["product_name"])
    out = {}
    for e in rest:
        unit = (e.get("bsn_unit") or "").strip()
        out[e["product_id"]] = f"{base} ({unit})" if unit else base
    return out


# ---------------------------------------------------------------------------
# 3. Collision detection
# ---------------------------------------------------------------------------

def find_collisions(proposed_by_pid: dict) -> list:
    by_name = defaultdict(list)
    for pid, name in proposed_by_pid.items():
        by_name[name].append(pid)
    return [{"name": n, "product_ids": sorted(ps)}
            for n, ps in by_name.items() if len(ps) > 1]


# ---------------------------------------------------------------------------
# 4. Generic-brand text anomaly
# ---------------------------------------------------------------------------

def find_generic_brand_text_anomalies(products: list, brand_lookup: dict) -> list:
    """Flags rows whose brand_id is a generic bucket (Other/No Name) but
    whose name literally contains ANOTHER real brand's name_th, corroborated
    by that brand having active siblings in the SAME category_id (guards
    against a coincidental substring match)."""
    by_cat_brand = defaultdict(set)
    for r in products:
        by_cat_brand[(r["category_id"], r["brand_id"])].add(r["id"])

    out = []
    for r in products:
        brec = brand_lookup.get(r["brand_id"])
        if not brec or brec.get("name") not in _GENERIC_BRAND_NAMES:
            continue
        name = r["product_name"]
        for bid, other in brand_lookup.items():
            if bid == r["brand_id"] or other.get("name") in _GENERIC_BRAND_NAMES:
                continue
            token = other.get("name_th") or ""
            if not token or token not in name:
                continue
            siblings = by_cat_brand.get((r["category_id"], bid))
            if siblings:
                out.append({
                    "product_id": r["id"], "current_name": name,
                    "current_brand_id": r["brand_id"],
                    "detected_brand_id": bid, "detected_brand_name": other.get("name"),
                    "sibling_count": len(siblings),
                })
                break
    return out


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_family_audit(db_path: Path) -> dict:
    conn = _connect_ro(db_path)
    color_rows = conn.execute("SELECT code, name_th FROM color_finish_codes").fetchall()
    color_codes = {r["code"]: r["name_th"] for r in color_rows}
    brand_rows = conn.execute("SELECT id, name, name_th, short_code FROM brands").fetchall()
    brand_lookup = {r["id"]: dict(r) for r in brand_rows}
    category_rows = conn.execute("SELECT id, name_th FROM categories").fetchall()
    category_names = {r["id"]: r["name_th"] for r in category_rows}

    products = conn.execute("""
        SELECT id, product_name, is_active, brand_id, category_id
          FROM products WHERE is_active = 1 ORDER BY id
    """).fetchall()
    products = [dict(r) for r in products]

    proposed_by_pid = {r["id"]: r["product_name"] for r in products}

    families = find_divergent_families(products, color_codes)
    family_rows, ambiguous_rows = [], []
    for fam in families:
        majority_sig = fam["majority_signature"]
        majority_rows = [r for r in fam["rows"] if fam["signatures"][r["id"]] == majority_sig]
        majority_prefix = Counter(
            leading_token(r["product_name"]) for r in majority_rows
        ).most_common(1)[0][0]
        default_unit = _infer_unit(majority_rows)
        brand_id = fam["brand_id"]
        brec = brand_lookup.get(brand_id) or {}
        family_key = f"cat{fam['category_id']}-brand{brand_id}-{majority_prefix}"

        for r in fam["rows"]:
            sig = fam["signatures"][r["id"]]
            if sig == majority_sig:
                continue
            kind = classify_divergence(sig, majority_sig)
            if kind == "size_format":
                brand_text = None
                for cand in (brec.get("name_th"), brec.get("name")):
                    if cand and cand in r["product_name"]:
                        brand_text = cand
                        break
                proposed, fix_kind = (None, "needs_manual_family_review")
                if brand_text:
                    proposed, fix_kind = propose_mechanical_fix(
                        r["product_name"], majority_prefix, brand_text, default_unit)
                family_rows.append({
                    "family_key": family_key, "product_id": r["id"], "is_active": 1,
                    "current_name": r["product_name"],
                    "proposed_name": proposed or "", "fix_kind": fix_kind,
                    "note": f"majority pattern in family: {majority_prefix!r} (brand {brec.get('name', '?')})",
                    "decision": "",
                })
                if proposed:
                    proposed_by_pid[r["id"]] = proposed
            else:
                ev = evidence_for_product(conn, r["id"])
                ambiguous_rows.append({
                    "batch_no": 1, "product_id": r["id"], "is_active": 1,
                    "issue": "FAMILY_IDENTITY_GAP",
                    "current_name": r["product_name"],
                    "proposal": (
                        f"family (category_id={fam['category_id']}, brand_id={brand_id}) majority "
                        f"pattern has {'a #model' if majority_sig[0] else 'no #model'} and "
                        f"{'a color' if majority_sig[3] != 'none' else 'no color'} — this row's "
                        f"{'#model' if sig[0] else 'no #model'}/{'color' if sig[3] != 'none' else 'no color'} "
                        "differs; fixing would ADD identity info not present in this row's own name — "
                        "needs Put's judgment (no hard linkage to auto-derive it)"
                    ),
                    "rationale": f"bsn_raw_mode={ev['bsn_raw_mode']!r} last_sale={ev['last_sale']!r}",
                })

    twins = find_twins(conn)
    twin_rows = []
    for t in twins:
        entries = t["entries"]
        proposal_map = {} if t["aligned"] else propose_twin_inheritance(entries)
        for e in entries:
            twin_rows.append({
                "bsn_code": t["bsn_code"], "product_id": e["product_id"],
                "bsn_unit": e.get("bsn_unit") or "",
                "current_name": e["product_name"], "aligned": t["aligned"],
                "proposed_name": proposal_map.get(e["product_id"], ""),
                "decision": "",
            })
            if e["product_id"] in proposal_map:
                proposed_by_pid[e["product_id"]] = proposal_map[e["product_id"]]

    collisions = find_collisions(proposed_by_pid)
    anomalies = find_generic_brand_text_anomalies(products, brand_lookup)

    conn.close()
    return {
        "family_rows": family_rows,
        "ambiguous_rows": ambiguous_rows,
        "twin_rows": twin_rows,
        "collisions": collisions,
        "anomalies": anomalies,
        "total_active": len(products),
        "n_families_divergent": len(families),
        "category_names": category_names,
        "brand_lookup": brand_lookup,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_reports(results: dict, out_dir: Path, tag: str):
    out_dir.mkdir(parents=True, exist_ok=True)

    fam_path = out_dir / f"audit_family_divergence_{tag}.csv"
    with open(fam_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["family_key", "product_id", "is_active",
                                           "current_name", "proposed_name", "fix_kind",
                                           "note", "decision"])
        w.writeheader()
        w.writerows(results["family_rows"])

    amb_path = out_dir / f"audit_ambiguous_proposals_{tag}.csv"
    with open(amb_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["batch_no", "product_id", "is_active",
                                           "issue", "current_name", "proposal", "rationale"])
        w.writeheader()
        w.writerows(results["ambiguous_rows"])

    twin_path = out_dir / f"audit_twins_{tag}.csv"
    with open(twin_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["bsn_code", "product_id", "bsn_unit",
                                           "current_name", "aligned", "proposed_name", "decision"])
        w.writeheader()
        w.writerows(results["twin_rows"])

    coll_path = out_dir / f"audit_collisions_{tag}.csv"
    with open(coll_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["name", "product_ids", "decision"])
        w.writeheader()
        for c in results["collisions"]:
            w.writerow({"name": c["name"],
                        "product_ids": ";".join(str(p) for p in c["product_ids"]),
                        "decision": ""})

    anom_path = out_dir / f"audit_field_anomalies_{tag}.csv"
    with open(anom_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "current_name", "current_brand_id",
                                           "detected_brand_id", "detected_brand_name",
                                           "sibling_count", "decision"])
        w.writeheader()
        for a in results["anomalies"]:
            row = dict(a)
            row["decision"] = ""
            w.writerow(row)

    return fam_path, amb_path, twin_path, coll_path, anom_path


def write_family_review_md(results: dict, out_dir: Path, tag: str, data_version_note: str) -> Path:
    """Put-facing review doc, grouped per D5: one section per divergent
    family (before/after table), then twins / collisions / ambiguous /
    field-anomalies as separate sections."""
    cat_names = results["category_names"]
    brand_lookup = results["brand_lookup"]

    def cat_label(cat_id):
        return f"{cat_names.get(cat_id, '?')} (cat {cat_id})" if cat_id is not None else "(no category)"

    def brand_label(brand_id):
        if brand_id is None:
            return "(no brand)"
        b = brand_lookup.get(brand_id) or {}
        return f"{b.get('name', '?')} (brand {brand_id})"

    lines = []
    lines.append(f"# Product Naming Round 2 — Family Consistency Review ({tag})")
    lines.append("")
    lines.append("Read-only detector (`sendy_erp/scripts/audit_family_consistency.py`), "
                  "`is_active=1` scope only. Zero DB writes: " + data_version_note + ". "
                  "See `projects/product-naming-round2/plan.md` for decisions D1-D6.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    fix_counts = Counter(r["fix_kind"] for r in results["family_rows"])
    misaligned = sum(1 for r in results["twin_rows"] if not r["aligned"])
    lines.append(f"- Active products audited: {results['total_active']}")
    lines.append(f"- Divergent families: {results['n_families_divergent']} groups, "
                  f"{len(results['family_rows'])} rows needing a per-row look "
                  f"({fix_counts.get('mechanical_auto', 0)} auto-proposed reformat / "
                  f"{fix_counts.get('needs_manual_family_review', 0)} need Put's manual family review)")
    lines.append(f"- Ambiguous (identity gap, D2): {len(results['ambiguous_rows'])} rows — "
                  "fixing would ADD model/color info the row doesn't carry, no hard linkage to justify it")
    lines.append(f"- Twins (bsn_code shared by >1 active product): "
                  f"{len({r['bsn_code'] for r in results['twin_rows']})} codes / "
                  f"{len(results['twin_rows'])} rows, {misaligned} misaligned")
    lines.append(f"- Collisions (same canonical name, different products): {len(results['collisions'])}")
    lines.append(f"- Field anomalies (generic brand_id but name names a real sibling brand): "
                  f"{len(results['anomalies'])}")
    lines.append("")
    lines.append("**Triage note (D5):** approve `mechanical_auto` families per-family (one decision covers "
                  "the whole family); `needs_manual_family_review` families need a quick per-family scan — "
                  "several large ones below (e.g. the 23-row บานพับสแตนเลส cluster) are likely NOT naming "
                  "bugs at all, just Sendai hinge models that legitimately carry no explicit size token in "
                  "the name (the spec lives in the model number) — expect to bulk-park most of those.")
    lines.append("")

    # 1. Family divergence
    lines.append("## 1. Family divergence — per family (before / after)")
    lines.append("")
    by_family = defaultdict(list)
    for r in results["family_rows"]:
        by_family[r["family_key"]].append(r)

    def _family_sort_key(item):
        key, rows_ = item
        kinds = {r["fix_kind"] for r in rows_}
        return (0 if "mechanical_auto" in kinds else 1, -len(rows_), key)

    for family_key, rows_ in sorted(by_family.items(), key=_family_sort_key):
        kinds = Counter(r["fix_kind"] for r in rows_)
        badge = "mechanical_auto (batch-approvable)" if kinds.get("mechanical_auto") else "needs manual review"
        lines.append(f"### `{family_key}` — {len(rows_)} row(s), {badge}")
        lines.append("")
        lines.append(f"Note: {rows_[0]['note']}")
        lines.append("")
        lines.append("| product_id | current_name | proposed_name | fix_kind |")
        lines.append("|---|---|---|---|")
        for r in sorted(rows_, key=lambda x: x["product_id"]):
            lines.append(f"| {r['product_id']} | {r['current_name']} | {r['proposed_name'] or '—'} | {r['fix_kind']} |")
        lines.append("")

    # 2. Twins
    lines.append("## 2. Twins (bsn_code shared by >1 active product)")
    lines.append("")
    by_code = defaultdict(list)
    for r in results["twin_rows"]:
        by_code[r["bsn_code"]].append(r)
    for bsn_code, rows_ in sorted(by_code.items()):
        aligned = rows_[0]["aligned"]
        status = "already aligned" if aligned else "MISALIGNED — needs a per-row decision"
        lines.append(f"### bsn_code `{bsn_code}` — {status}")
        lines.append("")
        lines.append("| product_id | bsn_unit | current_name | proposed_name |")
        lines.append("|---|---|---|---|")
        for r in rows_:
            lines.append(f"| {r['product_id']} | {r['bsn_unit']} | {r['current_name']} | {r['proposed_name'] or '—'} |")
        lines.append("")
    lines.append("")

    # 3. Collisions
    lines.append("## 3. Collisions")
    lines.append("")
    if results["collisions"]:
        lines.append("| canonical name | product_ids |")
        lines.append("|---|---|")
        for c in results["collisions"]:
            lines.append(f"| {c['name']} | {', '.join(str(p) for p in c['product_ids'])} |")
    else:
        lines.append("None found — no active product's current or proposed name collides with another.")
    lines.append("")

    # 4. Ambiguous
    lines.append("## 4. Ambiguous (identity gap — D2, no hard linkage)")
    lines.append("")
    lines.append(f"Full per-row list ({len(results['ambiguous_rows'])} rows) in "
                  f"`audit_ambiguous_proposals_{tag}.csv`. Grouped below by family for a quick scan; "
                  "each row needs Put's per-row call (add the missing info / leave as-is / park).")
    lines.append("")
    fam_re = re.compile(r"category_id=(\S+), brand_id=(\S+)\)")
    by_fam_amb = defaultdict(list)
    for r in results["ambiguous_rows"]:
        m = fam_re.search(r["proposal"])
        key = m.groups() if m else ("?", "?")
        by_fam_amb[key].append(r)
    lines.append("| category | brand | rows | example |")
    lines.append("|---|---|---|---|")
    for (cat_id_s, brand_id_s), rows_ in sorted(by_fam_amb.items(), key=lambda kv: -len(kv[1])):
        cat_id = int(cat_id_s) if cat_id_s.lstrip('-').isdigit() else None
        brand_id = int(brand_id_s) if brand_id_s.lstrip('-').isdigit() else None
        example = rows_[0]["current_name"]
        lines.append(f"| {cat_label(cat_id)} | {brand_label(brand_id)} | {len(rows_)} | {example} |")
    lines.append("")

    # 5. Field anomalies
    lines.append("## 5. Field anomalies")
    lines.append("")
    if results["anomalies"]:
        lines.append("| product_id | current_name | current_brand_id | detected_brand | sibling_count |")
        lines.append("|---|---|---|---|---|")
        for a in results["anomalies"]:
            lines.append(f"| {a['product_id']} | {a['current_name']} | {a['current_brand_id']} | "
                          f"{a['detected_brand_name']} (id {a['detected_brand_id']}) | {a['sibling_count']} |")
    else:
        lines.append("None found.")
    lines.append("")

    md_path = out_dir / f"family_review_{tag}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return md_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    before_mtime = args.db.stat().st_mtime
    before_conn = _connect_ro(args.db)
    before_dv = before_conn.execute("PRAGMA data_version").fetchone()[0]
    before_conn.close()

    results = run_family_audit(args.db)

    after_mtime = args.db.stat().st_mtime
    after_conn = _connect_ro(args.db)
    after_dv = after_conn.execute("PRAGMA data_version").fetchone()[0]
    after_conn.close()
    if before_mtime != after_mtime or before_dv != after_dv:
        sys.exit("REFUSING TO CONTINUE: DB changed during audit "
                  f"(mtime {before_mtime}->{after_mtime}, data_version {before_dv}->{after_dv}) "
                  "— should be impossible with read-only connections.")

    paths = write_reports(results, args.output_dir, args.date)
    dv_note = f"`PRAGMA data_version` {before_dv} -> {after_dv} (unchanged)"
    md_path = write_family_review_md(results, args.output_dir, args.date, dv_note)

    print(f"Active products audited:     {results['total_active']}")
    print(f"Divergent families found:    {results['n_families_divergent']}")
    print(f"  family-divergence rows:    {len(results['family_rows'])}")
    fix_counts = Counter(r["fix_kind"] for r in results["family_rows"])
    for k, n in fix_counts.most_common():
        print(f"    {k:<28} {n}")
    print(f"Ambiguous (identity gap):    {len(results['ambiguous_rows'])}")
    print(f"Twins (bsn_code shared):     {len({r['bsn_code'] for r in results['twin_rows']})} codes, "
          f"{len(results['twin_rows'])} rows")
    misaligned = sum(1 for r in results['twin_rows'] if not r['aligned'])
    print(f"  misaligned twin rows:      {misaligned}")
    print(f"Collisions:                  {len(results['collisions'])}")
    print(f"Field anomalies:             {len(results['anomalies'])}")
    print(f"data_version:                {before_dv} -> {after_dv} (unchanged)")
    print()
    for p in paths:
        print(f"Report: {p}")
    print(f"Report: {md_path}")


if __name__ == "__main__":
    main()
