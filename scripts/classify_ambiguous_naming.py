"""Deterministic classifier for audit_ambiguous_proposals_2026-07-21.csv
(the 213-row FAMILY_IDENTITY_GAP output from audit_family_consistency.py).

Every verdict below is derived from the rules encoded here — never from prose
judgment. Grounded in sendy_erp/docs/product_name_naming_rule.md (rule 4/5/17
for model, rule 9/19 for color) and round-1's documented COLOR_MISMATCH
keep-precedents (2026-07-07, audit_ambiguous_proposals_2026-07-07.csv batches
4-5). Read-only: the only DB access is the color_finish_codes lookup and the
BSN raw oracle query, both opened with mode=ro.

Rules (also emitted verbatim into the .md report):

  R1 (missing-model false positive)
      The proposal text flags a model-axis difference (majority has a #model,
      this row's own descriptor says none, or vice versa) AND current_name
      already contains a '#'-token (rule 5) or a bare model-like code (rule 17,
      letters+digits) -> REJECT. The row already has a model; the "gap" was a
      representation-style difference from the family majority, not a real
      absence.

  R2 (missing-color false positive)
      The proposal text flags a color-axis difference AND current_name already
      shows a color signal, checked in this order:
        R2a  a '(CODE)' bracket where CODE is an actual color_finish_codes.code
             (queried live from the DB, not hardcoded)
        R2b  literal 'สี' immediately followed by any Thai character — catches
             ANY compound color phrase, coded or not (e.g. 'สีดำด้าน', 'สีใส')
        R2c  a bare rule-19 token or a rule-doc pattern/texture token, matched
             with Thai-vowel-safe boundaries (explicit char-class boundaries,
             not \\b, so Thai vowel marks don't create a false boundary)
        R2d  a compound containing an R2c token that ISN'T independently
             boundary-safe, but the EXACT compound is one of round-1's
             documented 2026-07-07 KEEP/close-finding precedents (cited below)
      -> REJECT if any of R2a-R2d match.

  R3 (oracle check for the genuinely-unresolved axis/axes)
      For rows where R1/R2 don't resolve every flagged axis, query BSN raw
      (product_code_mapping -> purchase_transactions preferred, sales_transactions
      as fallback, marketplace customers excluded) for this specific product_id:
        - raw text itself shows a model/color signal not in current_name
          -> RECOMMEND-APPROVE (cites the raw fragment)
        - zero transaction history at all (purchase AND sales empty)
          -> RECOMMEND-PARK
        - raw exists but doesn't clearly confirm or deny the missing element
          -> ASK-PUT (never approve on unclear evidence — no hedging allowed
             to produce a recommend-approve verdict)

CLI:
    python classify_ambiguous_naming.py
    python classify_ambiguous_naming.py --db /path/to/inventory.db
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path.home() / "Sendai-Boonsawat" / "sendy_erp" / "inventory_app" / "instance" / "inventory.db"
REPORTS_DIR = Path.home() / "Sendai-Boonsawat" / "Operations" / "05_analysis-reports" / "data-quality" / "product-naming"
DEFAULT_IN = REPORTS_DIR / "audit_ambiguous_proposals_2026-07-21.csv"

# ---------------------------------------------------------------------------
# R1: model signal
# ---------------------------------------------------------------------------

_HASH_MODEL_RE = re.compile(r"#\S+")
# Bare model/kit code per rule 17 (letters + digits, optional dash) — loose
# on purpose (oracle raw text is messier than stored names).
_BARE_MODEL_RE = re.compile(r"\b[A-Za-z]{1,6}-?\d{2,5}\b")


def has_model_signal(text: str) -> bool:
    return bool(_HASH_MODEL_RE.search(text) or _BARE_MODEL_RE.search(text))


# ---------------------------------------------------------------------------
# R2: color signal
# ---------------------------------------------------------------------------

# Thai-vowel-safe boundaries: an explicit punctuation/space/edge char class
# instead of \b, so Thai vowel marks (which \b treats as non-word-adjacent in
# unpredictable ways) don't create a false boundary or a false non-match.
_BOUND_BEFORE = r"(?:^|(?<=[\s'\"()\[\].,;:/-]))"
_BOUND_AFTER = r"(?:$|(?=[\s'\"()\[\].,;:/-]))"

# Rule-19 bare color tokens, verbatim from product_name_naming_rule.md's
# "Bare colors (ไม่มี code)" table.
BARE_COLOR_TOKENS = [
    "ดำ", "Black", "ขาว", "White", "แดง", "Red", "น้ำเงิน", "Blue", "เขียว", "Green",
    "เหลือง", "Yellow", "น้ำตาล", "Brown", "ทอง", "เงิน", "ฟ้า", "ชา", "งา", "เทา",
    "ธรรมชาติ", "Nature",
]
# Patterns/textures from the rule doc's "Patterns / Textures" section — not a
# color, but a rule-19-adjacent finish descriptor recognized directly in the name.
PATTERN_TOKENS = ["ลายฆ้อน", "ลายคราม"]

# Round-1's documented COLOR_MISMATCH keep / close-finding precedents
# (2026-07-07, audit_ambiguous_proposals_2026-07-07.csv batches 4-5). Each is a
# compound where the bare rule-19 token IS present but glued to adjacent Thai
# text on at least one side, so R2c's strict boundary match misses it — these
# exact compounds were already adjudicated as "color already present, no
# change" and are reused verbatim, never invented fresh.
KEEP_PRECEDENT_COMPOUNDS = {
    "ด้ามขาว": "pid 417/418/748-754, batch 5 (2026-07-07): KEEP color_code=WHT",
    "ขนขาว": "pid 755/756/1680/1683-1685, batch 4 (2026-07-07): bare color already present",
    "ด้ามดำ": "pid 815, batch 4 (2026-07-07): bare color already present",
    "ขยะดำ": "pid 1357/1814, batch 4 (2026-07-07): bare color already present",
    "ไฟเขียว": "pid 1632/1633, batch 4 (2026-07-07): bare color already present",
    "พุกเขียว": "pid 530-532/535, batch 5 (2026-07-07): KEEP color_code=GRN",
    "ขอบแดง": "pid 1359, batch 5 (2026-07-07): KEEP color_code=RED",
    "ขอบเขียว": "pid 1360, batch 5 (2026-07-07): KEEP color_code=GRN",
    "ขอบเหลือง": "pid 1362, batch 5 (2026-07-07): KEEP color_code=YEL",
    "ฟ้าทึบ": "pid 1420, batch 5 (2026-07-07): KEEP color_code=SKY",
    "ด้ามส้ม": "pid 1524, batch 5 (2026-07-07): KEEP color_code=ORG",
    "น้ำเงินหัวเดียว": "pid 600-603, batch 4 (2026-07-07): bare color already present ('ด้ามใส-น้ำเงินหัวเดียว')",
}

_SI_THAI_RE = re.compile(r"สี[ก-๙]")


def color_signal(text: str, color_codes: set) -> tuple:
    """Returns (matched: bool, rule_id: str|None, detail: str)."""
    for code in color_codes:
        if f"({code})" in text:
            return True, "R2a", f"bracket ({code}) matches color_finish_codes.code"
    m = _SI_THAI_RE.search(text)
    if m:
        return True, "R2b", f"'สี'+Thai compound present at {m.group(0)!r}"
    for tok in BARE_COLOR_TOKENS + PATTERN_TOKENS:
        patt = re.compile(rf"{_BOUND_BEFORE}{re.escape(tok)}{_BOUND_AFTER}", re.IGNORECASE)
        if patt.search(text):
            return True, "R2c", f"bare rule-19/pattern token {tok!r} present with a valid boundary"
    for compound, citation in KEEP_PRECEDENT_COMPOUNDS.items():
        if compound in text:
            return True, "R2d", f"compound {compound!r} matches documented precedent: {citation}"
    return False, None, ""


# ---------------------------------------------------------------------------
# Proposal-text axis parser
# ---------------------------------------------------------------------------

_MAJ_RE = re.compile(r"majority pattern has (a #model|no #model) and (a color|no color)")


def parse_majority(proposal: str):
    """Only the MAJORITY-side descriptors are parsed from text — reliable and
    unambiguous ('a #model'/'no #model', 'a color'/'no color'). The row-side
    descriptors in the same sentence are NOT used: they collapse color_repr's
    3 states ('coded'/'bare'/'none') into a 2-state "color"/"no color" text,
    so two rows that differ only in REPRESENTATION style (bare vs coded, both
    non-'none') print identical row-side text and silently fail to parse as
    "differing" — a real bug caught during this classifier's own build (pid
    54 and others). The row's actual current state is instead checked FRESH
    against current_name via has_model_signal()/color_signal() below, which
    is both more robust and reuses the exact same R1/R2 logic."""
    m = _MAJ_RE.search(proposal)
    if not m:
        return None
    maj_model, maj_color = m.groups()
    return {"model": maj_model == "a #model", "color": maj_color == "a color"}


# ---------------------------------------------------------------------------
# R3: BSN raw oracle (product_code_mapping -> purchase preferred, sales fallback)
# ---------------------------------------------------------------------------

def bsn_oracle(conn: sqlite3.Connection, pid: int) -> dict:
    purch = [r[0] for r in conn.execute(
        "SELECT product_name_raw FROM purchase_transactions "
        "WHERE product_id = ? AND product_name_raw IS NOT NULL AND product_name_raw <> ''",
        (pid,))]
    sales = [r[0] for r in conn.execute(
        "SELECT product_name_raw FROM sales_transactions "
        "WHERE product_id = ? AND customer NOT LIKE 'หน้าร้าน%' "
        "AND product_name_raw IS NOT NULL AND product_name_raw <> ''",
        (pid,))]
    return {"purchase": purch, "sales": sales}


def oracle_verdict(oracle: dict, unresolved_axes: list, name: str, color_codes: set) -> tuple:
    """Returns (recommend, rule_id, evidence). Purchase preferred over sales;
    sales used only when purchase is empty (per team-lead's brief)."""
    raws = oracle["purchase"] if oracle["purchase"] else oracle["sales"]
    source = "purchase" if oracle["purchase"] else ("sales" if oracle["sales"] else "none")
    if not raws:
        return ("recommend-park", "R3-park",
                "zero BSN transaction history (purchase and sales, marketplace excluded) "
                "for this product_id")

    confirmed = []
    for raw in dict.fromkeys(raws):  # dedupe, keep order
        for axis in unresolved_axes:
            if axis == "model" and has_model_signal(raw) and not has_model_signal(name):
                confirmed.append(("model", raw))
            if axis == "color":
                ok, _rule, _detail = color_signal(raw, color_codes)
                if ok and not color_signal(name, color_codes)[0]:
                    confirmed.append(("color", raw))

    if confirmed:
        axes_confirmed = sorted({a for a, _ in confirmed})
        sample = confirmed[0][1]
        return ("recommend-approve", "R3-approve",
                f"BSN raw ({source}) confirms missing {'/'.join(axes_confirmed)}: {sample!r}")

    sample = list(dict.fromkeys(raws))[0]
    return ("ask-Put", "R3-askput",
            f"BSN raw ({source}, {len(raws)} row(s), e.g. {sample!r}) exists but does not "
            f"clearly confirm or deny the missing {'/'.join(unresolved_axes)}")


# ---------------------------------------------------------------------------
# Main classification
# ---------------------------------------------------------------------------

def classify_row(row: dict, color_codes: set, conn: sqlite3.Connection) -> dict:
    """Checks each axis the family MAJORITY has (model / color) against the
    row's CURRENT name, checked fresh — direction-agnostic on purpose. A row
    can be flagged either because it lacks something the majority has, or
    because it HAS something the majority lacks (the P1 detector's
    identity_missing bucket doesn't distinguish direction) — either way, if
    the row's current name already shows the signal, there is nothing to add
    for that axis, full stop. Only an axis the majority has AND the row's
    current name genuinely lacks goes to the R3 oracle check."""
    pid = int(row["product_id"])
    name = row["current_name"]
    maj = parse_majority(row["proposal"])
    if maj is None:
        return {"product_id": pid, "rule_fired": "PARSE_ERROR", "recommend": "ask-Put",
                "evidence": "could not parse majority model+color descriptors from proposal text"}

    row_has_model = has_model_signal(name)
    row_color_ok, row_color_rule, row_color_detail = color_signal(name, color_codes)

    fired, unresolved = [], []

    if maj["model"]:
        if row_has_model:
            fired.append(("R1", "current_name already has a model signal "
                                 "(#-token or bare model code); majority has a model too — "
                                 "nothing missing, any difference was representation-style only"))
        else:
            unresolved.append("model")

    if maj["color"]:
        if row_color_ok:
            fired.append((row_color_rule, row_color_detail + " (majority also has a color — "
                                                                "nothing missing, representation-style only)"))
        else:
            unresolved.append("color")

    if not maj["model"] and not maj["color"]:
        # Majority has neither — this row was flagged because IT has something
        # majority lacks (richer than the family baseline), never a "missing" case.
        has_extra = row_has_model or row_color_ok
        detail = (f"row has {'a model' if row_has_model else ''}"
                  f"{' and ' if row_has_model and row_color_ok else ''}"
                  f"{'a color' if row_color_ok else ''}" if has_extra else "no extra signal either")
        return {"product_id": pid, "rule_fired": "R1+R2-reverse", "recommend": "recommend-reject",
                "evidence": f"majority pattern has neither model nor color; {detail} — the row is "
                            "richer than (or equal to) the family baseline, not poorer; nothing to add"}

    if not unresolved:
        rule_fired = "+".join(r for r, _ in fired)
        evidence = "; ".join(d for _, d in fired)
        return {"product_id": pid, "rule_fired": rule_fired, "recommend": "recommend-reject",
                "evidence": evidence}

    # R3 for whatever remains, noting any partial R1/R2 resolution for the record.
    oracle = bsn_oracle(conn, pid)
    recommend, rule_id, evidence = oracle_verdict(oracle, unresolved, name, color_codes)
    if fired:
        prefix = "; ".join(f"{r} resolved: {d}" for r, d in fired)
        evidence = f"{prefix}. Remaining ({'/'.join(unresolved)}): {evidence}"
        rule_fired = "+".join([f for f, _ in fired] + [rule_id])
    else:
        rule_fired = rule_id
    return {"product_id": pid, "rule_fired": rule_fired, "recommend": recommend, "evidence": evidence}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--input", type=Path, default=DEFAULT_IN)
    ap.add_argument("--output-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--tag", default="2026-07-21")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")
    if not args.input.exists():
        sys.exit(f"Input CSV not found: {args.input}")

    rows_in = list(csv.DictReader(open(args.input, encoding="utf-8-sig")))
    source_pids = {int(r["product_id"]) for r in rows_in}

    before_mtime = args.db.stat().st_mtime
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    before_dv = conn.execute("PRAGMA data_version").fetchone()[0]

    color_codes = {r[0] for r in conn.execute("SELECT code FROM color_finish_codes")}

    results = [classify_row(r, color_codes, conn) for r in rows_in]

    after_dv = conn.execute("PRAGMA data_version").fetchone()[0]
    conn.close()
    after_mtime = args.db.stat().st_mtime
    if before_dv != after_dv or before_mtime != after_mtime:
        sys.exit("REFUSING TO CONTINUE: DB changed during classification "
                  f"(data_version {before_dv}->{after_dv}, mtime {before_mtime}->{after_mtime})")

    result_pids = {r["product_id"] for r in results}
    coverage_ok = (result_pids == source_pids) and (len(results) == len(rows_in))
    print(f"COVERAGE ASSERT: source_pids={len(source_pids)} result_pids={len(result_pids)} "
          f"result_rows={len(results)} -> {'PASS' if coverage_ok else 'FAIL'}")
    assert coverage_ok, (
        f"pid coverage mismatch: missing={source_pids - result_pids} "
        f"extra={result_pids - source_pids} dup_or_missing_rows={len(results) != len(rows_in)}"
    )

    out_csv = args.output_dir / f"prescreen_ambiguous_{args.tag}.csv"
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "rule_fired", "recommend", "evidence"])
        w.writeheader()
        w.writerows(results)

    print(f"data_version: {before_dv} -> {after_dv} (unchanged)")
    print(f"rows written: {len(results)} -> {out_csv}")
    counts = Counter(r["recommend"] for r in results)
    for k, n in counts.most_common():
        print(f"  {k:<20} {n}")
    rule_counts = Counter(r["rule_fired"] for r in results)
    print("rule_fired breakdown:")
    for k, n in rule_counts.most_common():
        print(f"  {k:<20} {n}")


if __name__ == "__main__":
    main()
