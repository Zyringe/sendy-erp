"""Compiles APPROVED rows from the round-2 naming-project decision-stamped
CSVs into the strict ops format `apply_product_naming.py` consumes (Phase 3
prep — product-naming-round2). Never modifies the apply engine; this script
only reads its contract (op ∈ name|field, fieldnames
op,product_id,field,value,before,after,source — see
`validate_ops`/`apply_ops`/`_FIELD_WHITELIST` in apply_product_naming.py) and
targets it exactly.

Input is a LIST of CSV paths (round-2's family_divergence / twins /
field_anomalies today; prescreen_rows / prescreen_ambiguous once they gain a
`decision` + an actionable-value column — see detect_kind()). Each file's
"kind" is auto-detected from its header row, not from its filename.

Rules:
  - only rows whose `decision` starts with "approved" (case-insensitive)
    compile into an op. Everything else (empty/keep/rejected/park) is
    SKIPPED but counted in the summary — never silently dropped from the
    accounting.
  - an approved row that can't be turned into a concrete op (missing
    proposed_name for a rename kind, missing detected_<field>_id for a field
    kind) is a FAIL-LOUD condition: the specific product_id is named in the
    error list and NOTHING is written to the ops output (all rows are still
    scanned first, so every offending row is reported in one pass, not just
    the first).
  - ops are deduped by (product_id, op, field) across files. Two ops sharing
    that key with DIFFERENT before/after/value are a CONFLICT -> fail loud.
    Two ops with DIFFERENT keys for the SAME product_id compose (e.g. pid 686:
    one `name` op from family_divergence + one `field` op from
    field_anomalies — deliberately two different files, two different keys,
    both survive).
  - a read-only DB stale-before check: for every op, the CURRENT DB value
    (product_name for a name op; the named column for a field op) must still
    match the CSV's recorded `before`/`current_*` value, and the product must
    still exist. A mismatch is a stale-before condition -> fail loud (SELECTs
    only; this script never writes to the DB).
  - a read-only proposed-name COLLISION guard (`check_name_collisions`): a
    `name` op's proposed name must not equal another ACTIVE product's
    current name (unless that product is itself renaming away in this same
    batch — a legal swap/chain), and two different products in the batch
    must not propose the SAME name. Either violation -> fail loud.
  - if ANY error (missing value / conflict / stale-before / name-collision)
    is found across the whole input set, the ops OUTPUT is empty (nothing
    partially compiles) — the errors list still names every offending row so
    they can all be fixed in one pass.

Output: an ops CSV (matching apply_product_naming.py's exact contract) plus a
printed summary: rows approved / not-approved / unstamped files, ops by type
(name / field), and how many field ops would trigger an sku_code regen
(reusing apply_product_naming.py::plan_sku_regen as a read-only PREVIEW —
name ops never affect sku_code, since build_sku_code reads structured
columns, not product_name).

CLI:
    python compile_round2_ops.py --db /path/to/inventory.db \\
        --input audit_family_divergence_2026-07-21.csv audit_twins_2026-07-21.csv \\
                audit_field_anomalies_2026-07-21.csv \\
        --out round2_ops_2026-07-21.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, namedtuple
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "inventory_app"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import apply_product_naming as apn  # noqa: E402  (reuse plan_sku_regen — read-only preview)
import parse_sku_names as psn        # noqa: E402  (reuse parse_name — the EXISTING name-parse
                                      # infrastructure; do not write a new parser, per plan P3/review item 8)
from bsn_suggest import _load_parser_context  # noqa: E402
from sku_code_utils import PACKAGING_SHORT     # noqa: E402

ExtractResult = namedtuple("ExtractResult", ["status", "op", "error_msg"])
CompileResult = namedtuple("CompileResult", ["ops", "errors", "summary"])


# ---------------------------------------------------------------------------
# Kind detection
# ---------------------------------------------------------------------------

def detect_kind(fieldnames) -> str:
    fields = set(fieldnames)
    if {"family_key", "proposed_name", "fix_kind"} <= fields:
        return "family_divergence"
    if {"bsn_code", "proposed_name", "aligned"} <= fields:
        return "twins"
    if {"detected_brand_id", "detected_brand_name"} <= fields:
        return "field_anomalies"
    # future-proofing: once prescreen_rows/prescreen_ambiguous gain a
    # decision-stamping convention, they'll be picked up here as long as they
    # carry EITHER a proposed_name column (rename) or an explicit field+value
    # pair (field change) alongside `decision` — never inferred from prose.
    #
    # round-2 fix ซ item 3: a SINGLE generic CSV may carry BOTH row shapes
    # (this is the shape the main thread stamps ALL future bucket approvals
    # in — judgment/mechanical/ambiguous/ไม่สวย strips/G-groups — one file,
    # not one kind-per-file). When the header has proposed_name AND field
    # AND value all present, the file-level kind can't distinguish them;
    # extract_op() resolves the ACTUAL kind per row instead.
    if "decision" in fields and {"proposed_name", "field", "value"} <= fields:
        return "generic_mixed"
    if "decision" in fields and "proposed_name" in fields:
        return "generic_name"
    if "decision" in fields and {"field", "value"} <= fields:
        return "generic_field"
    return "unstamped"


def is_approved(decision) -> bool:
    return bool(decision) and decision.strip().lower().startswith("approved")


# ---------------------------------------------------------------------------
# Per-kind op extraction
# ---------------------------------------------------------------------------

def extract_op(kind: str, row: dict, source_label: str) -> ExtractResult:
    decision = row.get("decision", "")
    if not is_approved(decision):
        return ExtractResult("not_approved", None, None)

    pid = row["product_id"]

    if kind == "generic_mixed":
        # Per-ROW shape resolution — delegate to the matching single-shape
        # kind's own logic below so there's exactly one place that builds
        # each op shape (no duplicated construction).
        has_name = bool((row.get("proposed_name") or "").strip())
        has_field = bool((row.get("field") or "").strip()) and bool((row.get("value") or "").strip())
        if has_name and has_field:
            return ExtractResult(
                "error", None,
                f"pid {pid}: decision starts with 'approved' but the row has BOTH a proposed_name "
                f"AND a field/value pair ({source_label}) — a generic_mixed row must be exactly "
                f"one kind; split it into two rows if both a rename and a field change are intended")
        if has_name:
            return extract_op("generic_name", row, source_label)
        if has_field:
            return extract_op("generic_field", row, source_label)
        return ExtractResult(
            "error", None,
            f"pid {pid}: decision starts with 'approved' but the row has neither a proposed_name "
            f"nor a field/value pair ({source_label}) — can't tell whether this is a rename or a "
            f"field change")

    if kind in ("family_divergence", "twins", "generic_name"):
        proposed = (row.get("proposed_name") or "").strip()
        if not proposed:
            return ExtractResult(
                "error", None,
                f"pid {pid}: decision starts with 'approved' but proposed_name is empty "
                f"({source_label}) — the decision text may describe a correction only in "
                f"prose; it must be reflected in proposed_name before this compiles")
        tag = row.get("family_key") if kind == "family_divergence" else row.get("bsn_code", "")
        op = {"op": "name", "product_id": pid, "field": "", "value": "",
              "before": (row.get("current_name") or "").strip(), "after": proposed,
              "source": f"{source_label}:{tag}"}
        return ExtractResult("approved", op, None)

    if kind == "field_anomalies":
        value = (row.get("detected_brand_id") or "").strip()
        if not value:
            return ExtractResult(
                "error", None,
                f"pid {pid}: decision starts with 'approved' but detected_brand_id is empty "
                f"({source_label})")
        op = {"op": "field", "product_id": pid, "field": "brand_id", "value": value,
              "before": (row.get("current_brand_id") or "").strip(), "after": value,
              "source": f"{source_label}:detected={row.get('detected_brand_name', '')}"}
        return ExtractResult("approved", op, None)

    if kind == "generic_field":
        field, value = row.get("field", ""), (row.get("value") or "").strip()
        if not field or not value:
            return ExtractResult(
                "error", None,
                f"pid {pid}: decision starts with 'approved' but field/value is incomplete "
                f"({source_label})")
        op = {"op": "field", "product_id": pid, "field": field, "value": value,
              "before": (row.get("before") or "").strip(), "after": value, "source": source_label}
        return ExtractResult("approved", op, None)

    raise ValueError(f"unknown kind {kind!r}")  # unreachable — unstamped never reaches here


# ---------------------------------------------------------------------------
# Dedupe + conflict detection
# ---------------------------------------------------------------------------

def _op_key(op: dict) -> tuple:
    return (op["product_id"], op["op"], op.get("field", ""))


def dedupe_and_check_conflicts(ops: list) -> tuple:
    """Groups ops by (product_id, op, field). Identical duplicates collapse to
    one; ops sharing a key with DIFFERING before/after/value are a conflict.
    Ops with different keys for the SAME product_id compose (both kept)."""
    by_key = {}
    for op in ops:
        by_key.setdefault(_op_key(op), []).append(op)

    final, conflicts = [], []
    for key, group in by_key.items():
        signatures = {(g["before"], g["after"], g["value"]) for g in group}
        if len(signatures) > 1:
            pid, op_type, field = key
            conflicts.append(
                f"pid {pid}: conflicting '{op_type}'"
                f"{f' ({field})' if field else ''} ops from different sources: "
                f"{[(g['source'], g['after'] or g['value']) for g in group]}")
            continue
        final.append(group[0])
    return final, conflicts


# ---------------------------------------------------------------------------
# Stale-before check (read-only)
# ---------------------------------------------------------------------------

_STALENESS_FIELDS = ("product_name", "brand_id", "color_code", "model",
                     "size", "packaging_th", "packaging_short", "series",
                     "condition", "pack_variant", "sub_category_short_code")
_STALENESS_COL_INDEX = {name: i for i, name in enumerate(_STALENESS_FIELDS)}


def check_staleness(db_path, ops: list) -> list:
    """Read-only. Every field op MUST carry a real 'before' (the DB's
    current value) or the literal 'NULL' sentinel (apply_ops's own
    NULL-current convention, mirrored here) — an empty/missing 'before' on a
    field op is a compiler bug, not a silently-skippable case (round-2 fix,
    code review item 7: the old bypass `if col_index is not None and
    op['before']:` waved through exactly this)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    errors = []
    try:
        cols = ", ".join(_STALENESS_FIELDS)
        for op in ops:
            pid = op["product_id"]
            row = conn.execute(f"SELECT {cols} FROM products WHERE id=?", (pid,)).fetchone()
            if row is None:
                errors.append(f"pid {pid}: not found in DB (stale/removed since the CSV was generated)")
                continue
            if op["op"] == "name":
                current = row[0]
                if current != op["before"]:
                    errors.append(
                        f"pid {pid}: stale before — DB product_name={current!r} but CSV recorded "
                        f"{op['before']!r}")
            elif op["op"] == "field":
                col_index = _STALENESS_COL_INDEX.get(op["field"])
                if col_index is None:
                    errors.append(f"pid {pid}: field {op['field']!r} has no staleness column mapping")
                    continue
                current = row[col_index]
                before = op["before"]
                if before == "":
                    errors.append(
                        f"pid {pid}: field op for {op['field']!r} has an empty 'before' — cannot "
                        f"verify staleness (use the 'NULL' sentinel if the current value is "
                        f"genuinely NULL, per apply_product_naming.py's own convention)")
                elif before == "NULL":
                    if current is not None:
                        errors.append(
                            f"pid {pid}: stale before — DB {op['field']}={current!r} but CSV "
                            f"recorded NULL")
                elif str(current) != before:
                    errors.append(
                        f"pid {pid}: stale before — DB {op['field']}={current!r} but CSV "
                        f"recorded {before!r}")
    finally:
        conn.close()
    return errors


# ---------------------------------------------------------------------------
# Proposed-name collision guard (round-2 fix ซ, item 2)
# ---------------------------------------------------------------------------

def check_name_collisions(db_path, name_ops: list) -> list:
    """Two read-only guards on approved 'name' ops' proposed (after) names:

    (a) EXTERNAL: a proposed name must not equal another ACTIVE product's
        CURRENT product_name in the live DB — UNLESS that other product is
        ITSELF being renamed away by its own 'name' op in this same batch.
        A same-batch swap/chain (pid A renames into pid B's current name
        while pid B renames to something else) is legal: by the time the
        whole batch applies, nobody still holds the colliding name. Only a
        name op counts for this exclusion — a product merely touched by a
        FIELD op in this batch keeps its current product_name, so a
        collision with it is still real.
    (b) INTRA-BATCH: two DIFFERENT product_ids in this batch must not
        propose the identical 'after' name — nothing in the DB adjudicates
        which one "wins" a shared final name.

    Returns a list of error strings, each naming the offending pids/name.
    Empty name_ops -> []."""
    if not name_ops:
        return []

    errors = []
    renamed_pids = {op["product_id"] for op in name_ops}

    by_after = {}
    for op in name_ops:
        by_after.setdefault(op["after"], set()).add(op["product_id"])
    for after, pids in by_after.items():
        if len(pids) > 1:
            errors.append(
                f"proposed name {after!r} is claimed by more than one product in this batch: "
                f"pids {sorted(pids)}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for op in name_ops:
            pid, after = op["product_id"], op["after"]
            rows = conn.execute(
                "SELECT id FROM products WHERE product_name=? AND is_active=1 AND id != ?",
                (after, pid)).fetchall()
            for (other_id,) in rows:
                if str(other_id) in renamed_pids:
                    continue  # legal same-batch swap — that product renames away too
                errors.append(
                    f"pid {pid}: proposed name {after!r} collides with ACTIVE pid {other_id}'s "
                    f"current product_name (pid {other_id} is not renaming in this batch)")
    finally:
        conn.close()
    return errors


# ---------------------------------------------------------------------------
# Structured-field sync (code review item 8): derive field ops from an
# approved rename's proposed_name via the EXISTING name-parse infrastructure
# (parse_sku_names.parse_name — the same parser audit_product_naming.py and
# bsn_suggest.py already use). Never a new parser.
# ---------------------------------------------------------------------------

# round-2 fix ค (re-review, item 1): size is the ONLY structured field
# confident enough to auto-emit. series / condition / pack_variant NEVER
# auto-emit — any difference from the DB's current value (in either
# direction: parser-empty-but-DB-has-something, OR parser-nonempty-but-
# different) always goes to manual review instead of an op, unconditionally
# (this supersedes the old size-widening-conditional distrust rule for
# series, and also neutralizes a same-batch brand-context nit where a
# differing brand from another op in the same batch could otherwise
# contaminate parse_name's series-context lookup for this one).
# sub_category_short_code is whitelisted in apply_product_naming.py for
# future MANUAL field ops but parse_name() has no field for it at all — it
# is never derived here, so there is nothing to restrict for it.
_SYNC_MANUAL_ONLY_FIELDS = ("series", "condition", "pack_variant")

# parse_name()'s size_re requires EACH 'AxB' segment to independently satisfy
# SIZE_SEG, and UNIT_GROUP anchors 'in' with \b — which fails whenever 'in' is
# immediately followed by another word char (e.g. the 'x' in '4inx3in').
# Net effect: a compound size where a leading segment has no unit of its own
# ('6x11in') OR where 'in' sits mid-compound ('4inx3inx2mm') only returns the
# LAST segment, silently dropping the rest. This regex finds one immediately-
# preceding '<digits>[<unit>]x' segment so it can be re-attached.
_PRECEDING_SIZE_SEG_RE = re.compile(
    r'(\d+(?:\.\d+)?(?:/\d+)?(?:in|mm|cm|นิ้ว|มิล)?[x×])$', re.IGNORECASE)


def _widen_truncated_size(proposed_name: str, parsed_size: str) -> tuple:
    """Repairs parse_name()'s size-parsing limitation described above — a
    string-level widening of the EXISTING parser's own output (walking
    backward from where the parsed value sits in the proposed name,
    re-absorbing any leading '<digits><unit?>x' segments it dropped), not a
    replacement parser. Returns (size, ambiguous).

    round-2 fix ค (re-review, item 2): `str.find()` anchors on the FIRST
    occurrence of `parsed_size` in `proposed_name`. If that substring occurs
    MORE THAN ONCE, there's no safe way to tell which occurrence the parser
    actually matched — e.g. 'รุ่น 2in ท่อ 4inx3inx2in': parse_name's
    leftmost-match search grabs the standalone '2in' near the start (a
    decoy), not the compound's own trailing '2in' segment, and widening from
    the wrong occurrence would silently fabricate a size. In that case this
    returns (parsed_size, True) — unrepaired — and the caller must treat the
    whole size as unreliable rather than guess."""
    if not parsed_size:
        return parsed_size, False
    if proposed_name.count(parsed_size) > 1:
        return parsed_size, True
    idx = proposed_name.find(parsed_size)
    if idx <= 0:
        return parsed_size, False
    widened, pos = parsed_size, idx
    while True:
        m = _PRECEDING_SIZE_SEG_RE.search(proposed_name[:pos])
        if not m:
            break
        widened = m.group(1) + widened
        pos = m.start()
    return widened, False


def derive_structured_sync_ops(name_ops: list, db_path) -> tuple:
    """For each approved 'name' op, parses the proposed (after) name and
    compares size/packaging_th/packaging_short against the DB's CURRENT
    value — these are the only fields confident enough to auto-emit. A
    confident, differing parse -> a field op (before = the live DB value, or
    the 'NULL' sentinel). A field the parser could NOT extract while the DB
    currently HAS a value there is AMBIGUOUS — round-1's own lesson (~42% of
    names intentionally diverge from a full rebuild-from-columns) means an
    empty parse is never treated as 'clear this field', only ever as 'needs
    a human to look' (fail-open: listed for manual review, never blocks the
    compile).

    series / condition / pack_variant NEVER auto-emit at all (round-2 fix ค,
    re-review item 1) — any difference from the DB's current value, in
    either direction, is unconditionally listed as ambiguous instead.

    Returns (sync_ops, ambiguous_notes)."""
    if not name_ops:
        return [], []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctx = _load_parser_context(conn)
        sync_ops, ambiguous = [], []
        for op in name_ops:
            pid = op["product_id"]
            row = conn.execute(
                "SELECT brand_id, size, series, condition, pack_variant, "
                "packaging_th, packaging_short FROM products WHERE id=?", (pid,)).fetchone()
            if row is None:
                continue  # reported elsewhere (staleness / missing product)
            brand_rec = ctx["brands_by_id"].get(row["brand_id"]) if row["brand_id"] else None
            parsed = psn.parse_name(op["after"], brand_rec, ctx["color_codes"],
                                     ctx["all_brand_tokens"], ctx["token_to_brand"])

            # size: the only structured field confident enough to auto-emit.
            raw_size = (parsed.get("size") or "").strip()
            size_ambiguous = False
            if raw_size:
                widened, size_ambiguous = _widen_truncated_size(op["after"], raw_size)
                parsed["size"] = widened
            db_size = row["size"]
            db_size_str = "" if db_size is None else str(db_size)
            if size_ambiguous:
                ambiguous.append(
                    f"pid {pid}: size — {raw_size!r} occurs more than once in the proposed name, "
                    f"can't safely tell which occurrence to widen from — manual review")
            elif not parsed["size"]:
                if db_size_str:
                    ambiguous.append(
                        f"pid {pid}: size — parser found no value in the proposed name but DB "
                        f"currently has {db_size_str!r} — manual review")
            elif parsed["size"] != db_size_str:
                sync_ops.append({
                    "op": "field", "product_id": pid, "field": "size", "value": parsed["size"],
                    "before": "NULL" if db_size is None else db_size_str, "after": parsed["size"],
                    "source": f"structured-sync:size:{op['source']}",
                })

            # series / condition / pack_variant: NEVER auto-emit (see
            # _SYNC_MANUAL_ONLY_FIELDS comment) — any difference from the
            # DB's current value always goes to manual review, unconditionally.
            for db_col in _SYNC_MANUAL_ONLY_FIELDS:
                parsed_val = (parsed.get(db_col) or "").strip()
                db_val = row[db_col]
                db_val_str = "" if db_val is None else str(db_val)
                if parsed_val != db_val_str:
                    ambiguous.append(
                        f"pid {pid}: {db_col} — parsed {parsed_val!r} differs from DB "
                        f"{db_val_str!r} — never auto-synced, manual review")

            # packaging: Thai value -> packaging_th, plus its derived packaging_short
            parsed_pkg = (parsed.get("packaging") or "").strip()
            db_pkg_th = row["packaging_th"]
            db_pkg_th_str = "" if db_pkg_th is None else str(db_pkg_th)
            if not parsed_pkg:
                if db_pkg_th_str:
                    ambiguous.append(
                        f"pid {pid}: packaging_th — parser found no packaging in the proposed "
                        f"name but DB currently has {db_pkg_th_str!r} — manual review")
                continue
            if parsed_pkg != db_pkg_th_str:
                sync_ops.append({
                    "op": "field", "product_id": pid, "field": "packaging_th", "value": parsed_pkg,
                    "before": "NULL" if db_pkg_th is None else db_pkg_th_str, "after": parsed_pkg,
                    "source": f"structured-sync:packaging_th:{op['source']}",
                })
            short = PACKAGING_SHORT.get(parsed_pkg)
            db_pkg_short = row["packaging_short"]
            db_pkg_short_str = "" if db_pkg_short is None else str(db_pkg_short)
            if not short:
                ambiguous.append(
                    f"pid {pid}: packaging_short — parsed packaging {parsed_pkg!r} has no known "
                    f"short code — manual review")
            elif short != db_pkg_short_str:
                sync_ops.append({
                    "op": "field", "product_id": pid, "field": "packaging_short", "value": short,
                    "before": "NULL" if db_pkg_short is None else db_pkg_short_str, "after": short,
                    "source": f"structured-sync:packaging_short:{op['source']}",
                })
        return sync_ops, ambiguous
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def compile_files(paths: list, db_path) -> CompileResult:
    all_ops, all_errors = [], []
    approved_count, not_approved_count, unstamped_files = 0, 0, 0

    for path in paths:
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        kind = detect_kind(rows[0].keys() if rows else [])
        label = Path(path).name
        if kind == "unstamped":
            unstamped_files += 1
            print(f"  {label}: not yet decision-stamped ({len(rows)} rows) — skipped, 0 ops")
            continue
        for row in rows:
            r = extract_op(kind, row, label)
            if r.status == "approved":
                approved_count += 1
                all_ops.append(r.op)
            elif r.status == "not_approved":
                not_approved_count += 1
            else:  # error
                all_errors.append(r.error_msg)

    deduped_ops, conflicts = dedupe_and_check_conflicts(all_ops)
    all_errors.extend(conflicts)

    name_ops = [o for o in deduped_ops if o["op"] == "name"]
    all_errors.extend(check_name_collisions(db_path, name_ops))

    # Derive structured-field sync ops from the (conflict-resolved) name ops,
    # then re-dedupe/conflict-check the COMBINED set — a sync op could in
    # principle collide with an explicit field op from another file.
    sync_ops, structured_sync_ambiguous = derive_structured_sync_ops(name_ops, db_path)
    combined_ops, sync_conflicts = dedupe_and_check_conflicts(deduped_ops + sync_ops)
    all_errors.extend(sync_conflicts)

    if combined_ops:
        all_errors.extend(check_staleness(db_path, combined_ops))

    final_ops = [] if all_errors else combined_ops
    ops_by_type = dict(Counter(o["op"] for o in final_ops))

    summary = {
        "approved": approved_count,
        "structured_sync_ambiguous": structured_sync_ambiguous,
        "not_approved": not_approved_count,
        "unstamped_files": unstamped_files,
        "ops_by_type": ops_by_type,
    }
    return CompileResult(final_ops, all_errors, summary)


def _sku_regen_implied_count(db_path, ops: list) -> int:
    """Read-only preview: how many of THIS batch's field ops would give their
    product a NEW sku_code (name ops never affect sku_code — it's built from
    structured columns, not product_name). Reuses
    apply_product_naming.plan_sku_regen unmodified, but plan_sku_regen scans
    the WHOLE catalog for drift (including pre-existing drift unrelated to
    this batch, e.g. the ~17 known rows from the P1 sku_drift audit) — so the
    result is filtered down to only the product_ids THIS batch touches,
    otherwise the count silently includes unrelated catalog-wide drift."""
    field_overrides = {}
    for op in ops:
        if op["op"] == "field":
            field_overrides.setdefault(op["product_id"], {})[op["field"]] = op["value"]
    if not field_overrides:
        return 0
    plans = apn.plan_sku_regen(db_path, field_overrides)
    touched = set(field_overrides)
    return len([p for p in plans if str(p["product_id"]) in touched])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--input", required=True, nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    print(f"Reading {len(args.input)} input file(s):")
    result = compile_files([str(p) for p in args.input], str(args.db))

    if result.errors:
        print(f"\nERRORS ({len(result.errors)}) — refusing to write any ops:")
        for e in result.errors:
            print(f"  - {e}")
        return 1

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["op", "product_id", "field", "value", "before", "after", "source"])
        w.writeheader()
        w.writerows(result.ops)

    regen_count = _sku_regen_implied_count(str(args.db), result.ops)

    print(f"\nSummary:")
    print(f"  approved (compiled):     {result.summary['approved']}")
    print(f"  not approved (skipped):  {result.summary['not_approved']}")
    print(f"  unstamped files:         {result.summary['unstamped_files']}")
    print(f"  ops written:             {len(result.ops)} -> {args.out}")
    for op_type, n in result.summary["ops_by_type"].items():
        print(f"    {op_type:<8} {n}")
    print(f"  sku-regen implied (preview, field ops only): {regen_count}")
    ambiguous = result.summary["structured_sync_ambiguous"]
    print(f"  structured-sync skipped, manual review: {len(ambiguous)}")
    for a in ambiguous:
        print(f"    - {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
