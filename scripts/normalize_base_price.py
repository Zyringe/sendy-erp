#!/usr/bin/env python3
"""Normalize the RAW catalog-pricing CSV into the schema import_catalog_pricing.py reads.

Rebuild of the lost one-shot `normalize_base_price.py`.

INPUT  (raw):  product_id, sku, sku_code, product_name, base_price, ราคาพิเศษ,
               โปรโมชั่น, Remark  (+ trailing empty cols)
OUTPUT (norm): the column set the importer reads (see OUTPUT_FIELDS below).

Crux logic
----------
base_price is messy: clean numbers, unit-suffixed ("230/โหล"), multi-tier
("40/แผง,360/โหล"), malformed ("560โหล"). The importer JOINs on product_id, so
we use the DB as an ANSWER KEY: each product's EXISTING base_sell_price (>0) and
cost_price disambiguate "as-is vs divided-by-ratio" per product. We never
silently overwrite an existing price with a value matching NEITHER candidate;
ambiguous rows get a BLANK base + a normalize_notes for review (importer no-ops).

Money math + parser → mandatory TDD. See tests/test_normalize_base_price.py.

This script ONLY writes the normalized CSV. It does NOT touch the live DB
(it reads products/unit_conversions read-only for the answer key).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional


# Exact column set the importer reads (verified against
# import_catalog_pricing.py::plan_writes_for_row + test_import_catalog_pricing.py).
OUTPUT_FIELDS = [
    "product_id", "sku_code", "product_name", "sendy_unit_type",
    "base_sell_price",
    "tier1_qty_label", "tier1_price", "tier1_note",
    "tier2_qty_label", "tier2_price", "tier2_note",
    "extra_tiers_json",
    "special_price",
    "promo_type", "promo_value",
    "bundle_buy", "bundle_free", "bundle_unit", "bundle_condition",
    "bundle_tiers_json",
    "gift_desc", "gift_qty",
    "normalize_notes",
]

# Canonical ratios fall-back when unit_conversions has no row for (pid, suffix).
# 1 suffix-unit = N × unit_type.
CANONICAL_RATIO = {
    "โหล": 12, "โหลคู่": 24, "กุรุส": 144, "กรุส": 144, "คู่": 2,
}

# Tolerance for answer-key matching (existing base_sell_price > 0).
ANSWER_KEY_TOL = 0.08  # 8%

# Sane-margin band when there is NO existing base: cost <= sell <= cost*MAX_MARGIN.
MAX_MARGIN = 4.0
# Below this cost we treat cost as junk/missing and refuse to guess a base.
MIN_TRUSTABLE_COST = 0.5


# ── number helpers ──────────────────────────────────────────────────────────

def _num(s):
    """Parse a number that may carry comma thousands separators. None if not numeric."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── base_price classification ───────────────────────────────────────────────

# A "<number>/<unit>" segment. The unit is captured lazily and stops at the
# next tier boundary: a comma, the start of the NEXT "<price>/" segment (so
# "40/แผง360/โหล" splits into แผง | โหล rather than swallowing 360), or end.
# The unit may itself start with digits (e.g. "1กิโล"), so we can't just forbid
# digits — the boundary is specifically "<digits>/" that begins a new segment.
_PRICE = r"\d[\d,]*(?:\.\d+)?"
_SEG_RE = re.compile(
    rf"({_PRICE})\s*/\s*(.+?)(?=\s*,|\s*{_PRICE}\s*/|$)")
_PURE_RE = re.compile(rf"^{_PRICE}$")


def classify_base_price(raw: str):
    """Return ('clean'|'suffixed'|'multi'|'malformed'|'blank', payload).

    payload:
      clean     -> float value
      suffixed  -> (value: float, suffix: str)
      multi     -> [(value, label), ...]   (>=2 priced segments)
      malformed -> raw string
      blank     -> None

    Multi-tier strings separate tiers by a comma OR by directly abutting the
    next "<price>/" segment, e.g. "85/1กิโล,1700/ลัง", "40/แผง,360/โหล" and
    "40/แผง360/โหล" all parse to two tiers. A bare comma-thousands number like
    "1,250.00" is still 'clean' because _PURE_RE matches it first.
    """
    s = (raw or "").strip()
    if not s:
        return "blank", None
    if _PURE_RE.match(s):
        return "clean", _num(s)

    # Scan the whole string for "<num>/<unit>" segments. The lazy unit + look-
    # ahead boundary handles both comma-separated and slash-abutting tiers.
    parsed = []
    for m in _SEG_RE.finditer(s):
        v = _num(m.group(1))
        lab = m.group(2).strip()
        if v is not None and lab:
            parsed.append((v, lab))

    if len(parsed) >= 2:
        return "multi", parsed
    if len(parsed) == 1:
        return "suffixed", (parsed[0][0], parsed[0][1])
    # No "<num>/<unit>" segment matched → malformed (e.g. "560โหล", "370 โหล").
    return "malformed", s


# ── base_price resolution ───────────────────────────────────────────────────

def _ratio_for(suffix: str, unit_type: str, uc_ratios: dict):
    """Resolve the suffix→unit_type ratio: unit_conversions first, then canonical,
    then 1 if suffix == unit_type, else None."""
    if suffix in uc_ratios:
        return uc_ratios[suffix]
    if suffix in CANONICAL_RATIO:
        return CANONICAL_RATIO[suffix]
    if suffix == unit_type:
        return 1.0
    return None


def resolve_base_price(raw: str, existing_base, cost, unit_type: str, uc_ratios: dict):
    """Resolve a raw base_price string into {base_sell_price, tiers, normalize_notes}.

    base_sell_price is a float or None (None = leave blank for review).
    tiers is a list of {qty_label, price, note}.
    """
    out = {"base_sell_price": None, "tiers": [], "normalize_notes": ""}

    kind, payload = classify_base_price(raw)

    if kind == "blank":
        return out

    if kind == "clean":
        out["base_sell_price"] = payload
        return out

    if kind == "multi":
        # Two+ prices → emit each as a tier. Decide base only if the
        # smallest-unit segment maps to unit_type (ratio 1); else blank+note.
        for val, label in payload:
            out["tiers"].append({"qty_label": label, "price": val, "note": None})
        base = None
        for val, label in payload:
            if _ratio_for(label, unit_type, uc_ratios) == 1.0:
                base = val
                break
        if base is not None:
            out["base_sell_price"] = base
            out["normalize_notes"] = (
                f"multi-tier {raw!r}: base from unit_type-matching segment; "
                f"others kept as tiers")
        else:
            out["normalize_notes"] = (
                f"multi-tier {raw!r}: no segment maps to unit_type "
                f"{unit_type!r}; base left blank, both kept as tiers")
        return out

    if kind == "malformed":
        out["normalize_notes"] = f"malformed base_price {raw!r}: could not parse; base left blank"
        return out

    # kind == "suffixed": (value, suffix)
    value, suffix = payload
    ratio = _ratio_for(suffix, unit_type, uc_ratios)
    candidates = {"as_is": value}
    if ratio:
        candidates["divided"] = value / ratio

    has_existing = existing_base is not None and existing_base > 0

    if has_existing:
        # Pick whichever candidate is closest to the existing base WITHIN tolerance.
        best_key, best_rel = None, None
        for k, v in candidates.items():
            rel = abs(v - existing_base) / existing_base
            if rel <= ANSWER_KEY_TOL and (best_rel is None or rel < best_rel):
                best_key, best_rel = k, rel
        if best_key is not None:
            out["base_sell_price"] = candidates[best_key]
        else:
            cand_str = ", ".join(f"{k}={round(v, 2)}" for k, v in candidates.items())
            out["normalize_notes"] = (
                f"suffixed {raw!r}: existing base {existing_base} matches neither "
                f"candidate ({cand_str}, ratio={ratio}); left blank to avoid bad overwrite")
        return out

    # No existing base: use cost band to pick a sane candidate.
    if cost is None or cost < MIN_TRUSTABLE_COST:
        cand_str = ", ".join(f"{k}={round(v, 2)}" for k, v in candidates.items())
        out["normalize_notes"] = (
            f"suffixed {raw!r}: no existing base and cost {cost} untrustworthy "
            f"({cand_str}, ratio={ratio}); base left blank for review")
        return out

    sane = {k: v for k, v in candidates.items() if cost <= v <= cost * MAX_MARGIN}
    if len(sane) == 1:
        out["base_sell_price"] = next(iter(sane.values()))
    else:
        cand_str = ", ".join(f"{k}={round(v, 2)}" for k, v in candidates.items())
        reason = "no candidate in cost band" if not sane else "multiple candidates in cost band"
        out["normalize_notes"] = (
            f"suffixed {raw!r}: {reason} (cost={cost}, {cand_str}, ratio={ratio}); "
            f"base left blank for review")
    return out


# ── special_price (ราคาพิเศษ) ────────────────────────────────────────────────

def parse_special_price(raw: str):
    """Return (special_price_or_None, promo_passthrough_or_None, note).

    Numeric → special_price. Non-numeric Thai text (e.g. 'ลด 10%') is actually a
    promo that landed in the wrong column → return it as promo_passthrough so the
    caller routes it through parse_promo.
    """
    s = (raw or "").strip()
    if not s:
        return None, None, ""
    val = _num(s)
    if val is not None:
        return val, None, ""
    return None, s, f"ราคาพิเศษ {s!r} is non-numeric; routed to promo parser"


# ── promo (โปรโมชั่น) ─────────────────────────────────────────────────────────

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# "ซื้อ 12 แถม 1", "10 แถม 1", "1แถม 1", "ซื้อ 12 ฟรี 1", "ซื้อ 120 ดอก แถม 12 ดอก"
_BUNDLE_RE = re.compile(
    r"(?:ซื้อ\s*)?(\d+)\s*(?:[ก-๙]+\s*)?(?:แถม|ฟรี)\s*(\d+)")
# gift wording: "แถม<thing>" where <thing> is NOT a bare bundle qty.
_GIFT_RE = re.compile(r"แถม\s*([^\d/].*?)(?:$|/)")


def _has_percent(t):
    m = _PERCENT_RE.search(t)
    return float(m.group(1)) if m else None


def _condition(t):
    if "ยกลัง" in t:
        return "ยกลัง"
    if "ยกล่อง" in t or "ยกกล่อง" in t:
        return "ยกล่อง"
    return None


# Free-text we deliberately refuse to classify as a promo (online-price notes,
# per-tier conditional pricing that has no clean structured shape).
_UNPARSEABLE_HINTS = ("ออนไลน์", "ชุดละ", "ใบละ", "ตัวละ", "กก.ละ", "อันละ", "บาท")


def parse_promo(raw: str):
    """Parse free-text Thai promo into the structured promo schema.

    Returns a dict with keys: promo_type, promo_value, bundle_buy, bundle_free,
    bundle_unit, bundle_condition, bundle_tiers_json, gift_desc, gift_qty,
    normalize_notes. promo_type='' means "could not classify".

    Emitted shapes are guaranteed to satisfy the promotions CHECK constraint
    (percent/fixed/bundle/gift/mixed).
    """
    blank = {
        "promo_type": "", "promo_value": None,
        "bundle_buy": None, "bundle_free": None, "bundle_unit": None,
        "bundle_condition": None, "bundle_tiers_json": None,
        "gift_desc": None, "gift_qty": None, "normalize_notes": "",
    }
    t = (raw or "").strip()
    if not t:
        return blank

    p = dict(blank)

    pct = _has_percent(t)
    bundles = _BUNDLE_RE.findall(t)
    cond = _condition(t)

    # F5: the promotions CHECK requires a percent discount_value BETWEEN 0 AND
    # 100. A parsed percent outside that range (e.g. "ลด 150%") would ROLLBACK
    # the whole single-transaction import. Treat it as NOT an emittable percent:
    # drop the value, never set promo_type='percent'/carry it into 'mixed', and
    # flag it for review. Any co-occurring bundle/gift still emits cleanly.
    pct_out_of_range = pct is not None and not (0 <= pct <= 100)
    oor_note = ""
    if pct_out_of_range:
        oor_note = f"percent out of range: {pct} — review (not emitted as a promo)"
        pct = None

    # Gift detection: "แถม X" where X is descriptive (not a bare bundle qty).
    # Avoid double-counting the bundle "แถม N" — strip matched bundle spans first.
    gift_desc = None
    gm = _GIFT_RE.search(t)
    if gm:
        cand = gm.group(1).strip()
        # If the candidate is purely a number+unit already captured as a bundle, skip.
        if cand and not re.fullmatch(r"\d+\s*[ก-๙]*", cand):
            gift_desc = cand

    has_pct = pct is not None
    has_bundle = len(bundles) >= 1
    has_gift = gift_desc is not None
    n_signals = int(has_pct) + int(has_bundle) + int(has_gift)

    # Multi-tier bundle (e.g. "ซื้อ 12 แถม 1/ ซื้อ 24 แถม 3") → keep all in JSON.
    bundle_tiers_json = None
    if len(bundles) >= 2:
        bundle_tiers_json = json.dumps(
            [{"buy": int(b), "free": int(f)} for b, f in bundles],
            ensure_ascii=False)

    def _with_oor(note):
        """Append the out-of-range-percent flag (if any) to a note."""
        if oor_note:
            return f"{note} | {oor_note}" if note else f"promo {t!r}: {oor_note}"
        return note

    if n_signals == 0:
        # Pure online-price note, unrecognized text, or ONLY an out-of-range
        # percent → refuse to emit a promo.
        p["promo_type"] = ""
        if oor_note:
            p["normalize_notes"] = f"promo {t!r}: {oor_note}"
        else:
            p["normalize_notes"] = f"promo {t!r}: could not classify confidently; left blank for review"
        return p

    if n_signals == 1 and has_pct:
        p["promo_type"] = "percent"
        p["promo_value"] = pct
        p["bundle_condition"] = cond
        note = f"promo {t!r}: percent {pct}% with condition {cond}" if cond else ""
        p["normalize_notes"] = _with_oor(note)
        return p

    if n_signals == 1 and has_bundle:
        buy, free = int(bundles[0][0]), int(bundles[0][1])
        p["promo_type"] = "bundle"
        p["bundle_buy"] = buy
        p["bundle_free"] = free
        p["bundle_condition"] = cond
        p["bundle_tiers_json"] = bundle_tiers_json
        note = f"promo {t!r}: multi-tier bundle (first tier in buy/free, all in JSON)" if bundle_tiers_json else ""
        p["normalize_notes"] = _with_oor(note)
        return p

    if n_signals == 1 and has_gift:
        # gift requires gift_qty NOT NULL per CHECK. Try to extract a qty.
        qty = None
        qm = re.search(r"(\d+)\s*[ก-๙]*\s*$", gift_desc)
        if qm:
            qty = qm.group(1)
        p["promo_type"] = "gift"
        p["gift_desc"] = gift_desc
        p["gift_qty"] = qty if qty is not None else "1"
        note = f"promo {t!r}: gift qty not stated; defaulted gift_qty=1 — review" if qty is None else ""
        p["normalize_notes"] = _with_oor(note)
        return p

    # n_signals >= 2 → mixed. mixed CHECK: at least one of discount_value/
    # bundle_buy/gift_desc populated; any combination valid.
    p["promo_type"] = "mixed"
    if has_pct:
        p["promo_value"] = pct
    if has_bundle:
        p["bundle_buy"] = int(bundles[0][0])
        p["bundle_free"] = int(bundles[0][1])
        p["bundle_tiers_json"] = bundle_tiers_json
    if has_gift:
        p["gift_desc"] = gift_desc
        qm = re.search(r"(\d+)\s*[ก-๙]*\s*$", gift_desc)
        p["gift_qty"] = qm.group(1) if qm else "1"
    p["bundle_condition"] = cond
    note = f"promo {t!r}: classified mixed (signals: " + ", ".join(
        s for s, ok in [("percent", has_pct), ("bundle", has_bundle), ("gift", has_gift)] if ok) + ")"
    p["normalize_notes"] = _with_oor(note)
    return p


# ── row normalization ───────────────────────────────────────────────────────

def _s(x):
    """Render a value for CSV: None/'' → '', floats via repr-ish, ints as-is."""
    if x is None or x == "":
        return ""
    if isinstance(x, float):
        return str(x)
    return str(x)


def normalize_row(row: dict, existing_base, cost, unit_type: str, uc_ratios: dict):
    """Produce a full normalized output dict (all OUTPUT_FIELDS as strings)."""
    out = {k: "" for k in OUTPUT_FIELDS}
    out["product_id"] = (row.get("product_id") or "").strip()
    out["sku_code"] = (row.get("sku_code") or "").strip()
    out["product_name"] = (row.get("product_name") or "").strip()
    out["sendy_unit_type"] = unit_type or ""

    notes = []

    # ── base_price ──
    base_res = resolve_base_price(
        row.get("base_price", ""), existing_base, cost, unit_type, uc_ratios)
    if base_res["base_sell_price"] is not None:
        out["base_sell_price"] = _s(base_res["base_sell_price"])
    if base_res["normalize_notes"]:
        notes.append(base_res["normalize_notes"])

    tiers = base_res["tiers"]
    if tiers:
        out["tier1_qty_label"] = _s(tiers[0]["qty_label"])
        out["tier1_price"] = _s(tiers[0]["price"])
        out["tier1_note"] = _s(tiers[0]["note"])
    if len(tiers) >= 2:
        out["tier2_qty_label"] = _s(tiers[1]["qty_label"])
        out["tier2_price"] = _s(tiers[1]["price"])
        out["tier2_note"] = _s(tiers[1]["note"])
    if len(tiers) >= 3:
        out["extra_tiers_json"] = json.dumps(
            [{"qty_label": t["qty_label"], "price": t["price"], "note": t["note"]}
             for t in tiers[2:]], ensure_ascii=False)

    # ── ราคาพิเศษ ──
    sp, promo_passthru, sp_note = parse_special_price(row.get("ราคาพิเศษ", ""))
    if sp is not None:
        out["special_price"] = _s(sp)
    if sp_note:
        notes.append(sp_note)

    # ── โปรโมชั่น (+ any non-numeric ราคาพิเศษ passthrough) ──
    promo_text = (row.get("โปรโมชั่น") or "").strip()
    if not promo_text and promo_passthru:
        promo_text = promo_passthru
    elif promo_text and promo_passthru:
        # both present: keep the main promo column; note the passthrough
        notes.append(f"also saw promo-like ราคาพิเศษ {promo_passthru!r} (used โปรโมชั่น)")

    if promo_text:
        pr = parse_promo(promo_text)
        out["promo_type"] = pr["promo_type"]
        out["promo_value"] = _s(pr["promo_value"])
        out["bundle_buy"] = _s(pr["bundle_buy"])
        out["bundle_free"] = _s(pr["bundle_free"])
        out["bundle_unit"] = _s(pr["bundle_unit"])
        out["bundle_condition"] = _s(pr["bundle_condition"])
        out["bundle_tiers_json"] = _s(pr["bundle_tiers_json"])
        out["gift_desc"] = _s(pr["gift_desc"])
        out["gift_qty"] = _s(pr["gift_qty"])
        if pr["normalize_notes"]:
            notes.append(pr["normalize_notes"])

    out["normalize_notes"] = " | ".join(notes)
    return out


# ── driver ──────────────────────────────────────────────────────────────────

def load_answer_key(db_path: Path):
    """Read products (base/cost/unit_type) + unit_conversions ratios from DB (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    prod = {}
    for r in conn.execute("SELECT id, base_sell_price, cost_price, unit_type FROM products"):
        prod[r["id"]] = {
            "base": r["base_sell_price"], "cost": r["cost_price"],
            "unit_type": r["unit_type"] or "ตัว",
        }
    uc = {}
    for r in conn.execute("SELECT product_id, bsn_unit, ratio FROM unit_conversions"):
        uc.setdefault(r["product_id"], {})[r["bsn_unit"]] = r["ratio"]
    conn.close()
    return prod, uc


def run(in_csv: Path, out_csv: Path, db_path: Path, verbose: bool = True):
    prod, uc = load_answer_key(db_path)
    rows = list(csv.DictReader(open(in_csv, encoding="utf-8")))

    # F6: detect duplicate product_ids across input rows. The importer JOINs on
    # product_id, so two rows for the same pid → last-wins silent overwrite. Flag
    # every dup row in normalize_notes AND print a summary so a human resolves
    # before import. Only integer pids can collide on the importer's join.
    pid_counts = Counter(
        (r.get("product_id") or "").strip() for r in rows
        if (r.get("product_id") or "").strip().isdigit())
    dup_pids = {pid: n for pid, n in pid_counts.items() if n > 1}

    out_rows = []
    skipped_pid = 0
    for r in rows:
        pid_raw = (r.get("product_id") or "").strip()
        if not pid_raw.isdigit():
            skipped_pid += 1
            # still emit the row so the importer's non-int skip path is fed the data
            out_rows.append(normalize_row(r, None, None, "", {}))
            continue
        pid = int(pid_raw)
        info = prod.get(pid)
        if info is None:
            o = normalize_row(r, None, None, "", {})
        else:
            o = normalize_row(
                r, info["base"], info["cost"], info["unit_type"], uc.get(pid, {}))
        if pid_raw in dup_pids:
            dup_note = f"DUPLICATE product_id {pid_raw} — {dup_pids[pid_raw]} rows (resolve before import; last-wins overwrite otherwise)"
            o["normalize_notes"] = (
                f"{dup_note} | {o['normalize_notes']}" if o["normalize_notes"] else dup_note)
        out_rows.append(o)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        for o in out_rows:
            w.writerow(o)

    if verbose:
        print(f"Read {len(rows)} raw rows → wrote {len(out_rows)} normalized rows to {out_csv}")
        print(f"  rows with non-integer product_id (importer will skip): {skipped_pid}")
        if dup_pids:
            total_dup_rows = sum(dup_pids.values())
            print(f"  ⚠ WARNING: {len(dup_pids)} duplicate product_id value(s) "
                  f"across {total_dup_rows} rows — RESOLVE before import "
                  f"(last-wins overwrite otherwise):")
            for pid_raw, n in sorted(dup_pids.items(), key=lambda kv: int(kv[0])):
                print(f"      duplicate product_id {pid_raw}: {n} rows")
    return out_rows


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="in_csv", required=True, type=Path,
                    help="Raw catalog CSV")
    ap.add_argument("--out", dest="out_csv", required=True, type=Path,
                    help="Normalized CSV to write")
    ap.add_argument("--db", type=Path,
                    default=here.parent / "inventory_app/instance/inventory.db",
                    help="Sendy DB for the answer key (read-only)")
    args = ap.parse_args()
    run(args.in_csv.resolve(), args.out_csv.resolve(), args.db.resolve())


if __name__ == "__main__":
    main()
