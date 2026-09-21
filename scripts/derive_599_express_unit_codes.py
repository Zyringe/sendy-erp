"""Derive migration 190's Express evidence: which stock codes BSN5657 bills in
`กร`, `ถง` or `บล` (#599, spec #595, ADR 0018).

Migration 190 gives those three codes Express's own meaning (กุรุส / ถัง /
บล็อก) and, in the same change, gives every affected product a conversion under
the NEW word at the ratio the OLD reading resolves to. "Affected" is the union
of two sources:

  * a product holding a `unit_conversions` row keyed on the raw code — visible
    inside the DB, so the migration derives that arm itself;
  * a product Express BILLS under the code — invisible inside Sendy, because
    the importer already translated the code away on the way in. That arm is
    this script's output: the (code, stock code) pairs the migration embeds and
    resolves through `product_code_mapping` at run time.

READ-ONLY. It never writes to the DBF or to Sendy; it prints SQL for a human to
paste into the migration, and re-prints the counts so a reviewer can diff them
against the ones recorded in the migration header.

    EXPRESS_DIR=~/Sendai-Boonsawat/projects/express-integration/data/BSN5657 \\
    ~/.virtualenvs/erp/bin/python scripts/derive_599_express_unit_codes.py

The pairs cannot be re-derived inside the pytest suite: the BSN5657 snapshot
lives outside this repo (it is ~120MB of DBF in the brain repo) and CI has no
copy. tests/test_migration_190_express_unit_meanings.py therefore pins the
SHAPE of the embedded list (three codes, every row non-empty, the codes Sendy
actually holds a conversion for are covered) and says so in its docstring — the
list's CONTENT is only as good as the run of this script recorded in the PR.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "inventory_app"))
import express_dbf_source as eds  # noqa: E402

CODES = ('กร', 'ถง', 'บล')
DEFAULT_DIR = os.path.expanduser(
    "~/Sendai-Boonsawat/projects/express-integration/data/BSN5657")
DATASET_DIR = os.environ.get("EXPRESS_DIR", DEFAULT_DIR)


def main():
    if not os.path.isdir(DATASET_DIR):
        print(f"BSN5657 snapshot not found at {DATASET_DIR} (set EXPRESS_DIR).")
        return 1
    lines = Counter()                       # (code, stkcod) -> n
    factors = defaultdict(Counter)          # (code, stkcod) -> factor -> n
    years = defaultdict(Counter)            # code -> year -> n
    for r in eds.open_table(DATASET_DIR, "STCRD"):
        code = (r.get("TQUCOD") or "").strip()
        if code not in CODES:
            continue
        stk = (r.get("STKCOD") or "").strip()
        lines[(code, stk)] += 1
        factors[(code, stk)][r.get("TFACTOR")] += 1
        docdat = r.get("DOCDAT")
        years[code][docdat.year if docdat else None] += 1

    print(f"-- source: {DATASET_DIR}/STCRD.DBF")
    for code in CODES:
        pairs = [k for k in lines if k[0] == code]
        print(f"-- {code}: {len(pairs)} stock codes, {sum(lines[k] for k in pairs)} lines, "
              f"by year {dict(sorted(years[code].items(), key=lambda kv: str(kv[0])))}")
    print()
    print("INSERT INTO _mig190_stkcod (code, bsn_code) VALUES")
    rows = sorted(lines, key=lambda k: (CODES.index(k[0]), k[1]))
    for i, (code, stk) in enumerate(rows):
        end = ";" if i == len(rows) - 1 else ","
        fac = dict(sorted(factors[(code, stk)].items(), key=lambda kv: str(kv[0])))
        print(f"    ('{code}', '{stk}'){end}  -- {lines[(code, stk)]} lines, TFACTOR {fac}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
