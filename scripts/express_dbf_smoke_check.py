"""One-off, read-only smoke check for express_dbf_source.py (Phase 1 slice A).

Reproduces Phase 0's independent-signal check (projects/express-integration/
spike/phase0_crosscheck.py) but through THIS PR's actual adapter code
(build_sales_entries / build_purchase_entries), not the spike's ad-hoc script
— proves the production adapter, not just the spike, reconstructs Sendy's
existing sales_transactions/purchase_transactions doc set + net totals from
the DBF.

Read-only both sides: the Sendy DB is opened `mode=ro`; nothing is written to
the DBF or to Sendy. Skips (prints a message, exit 0) if the dataset isn't
mounted — this is a manual/optional check, not part of the pytest suite (the
golden rule there is tests never touch the live DB — see tests/conftest.py).

Run with the erp venv:
    EXPRESS_DIR=/Volumes/ZYRINGE_128/BSN5657 \\
    ~/.virtualenvs/erp/bin/python scripts/express_dbf_smoke_check.py
"""
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "inventory_app"))
import express_dbf_source as eds  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "inventory_app", "instance", "inventory.db")
DATASET_DIR = os.environ.get("EXPRESS_DIR", "/Volumes/ZYRINGE_128/BSN5657")

# Same test windows Phase 0 verified (MAPPING.md) — chosen so each contains
# every doc sub-class (IV/HS/SR or RR/HP/GR) the type must cover.
SALES_WINDOW = ("2026-01-01", "2026-04-30")
PURCHASE_WINDOW = ("2026-01-01", "2026-03-31")


def sendy_conn():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def r2(x):
    return round(x or 0.0, 2)


def three_way(dbf, sendy, label):
    matched = set(dbf) & set(sendy)
    dbf_only = set(dbf) - set(sendy)
    sendy_only = set(sendy) - set(dbf)
    mismatches = [(k, dbf[k], sendy[k]) for k in matched if abs(dbf[k] - sendy[k]) > 0.01]

    print(f"\n=== {label} ===")
    print(f"  DBF docs:   {len(dbf)}")
    print(f"  Sendy docs: {len(sendy)}")
    print(f"  matched:    {len(matched)} (exact={len(matched) - len(mismatches)}, mismatch={len(mismatches)})")
    print(f"  DBF-only (backlog, OK): {len(dbf_only)}")
    print(f"  Sendy-only (GAP, must be 0 or explained): {len(sendy_only)}")
    if mismatches:
        for k, d, s in sorted(mismatches, key=lambda t: -abs(t[1] - t[2]))[:10]:
            print(f"    mismatch {k}: dbf={d:.2f} sendy={s:.2f} diff={d - s:+.2f}")
    if sendy_only:
        print(f"  Sendy-only docs (first 20): {sorted(sendy_only)[:20]}")
    return len(sendy_only)


def main():
    if not os.path.isdir(DATASET_DIR):
        print(f"Dataset not mounted at {DATASET_DIR} (set EXPRESS_DIR) — skipping smoke check.")
        return 0

    artrn = eds.open_table(DATASET_DIR, "ARTRN")
    aptrn = eds.open_table(DATASET_DIR, "APTRN")
    stcrd = eds.open_table(DATASET_DIR, "STCRD")
    armas = eds.open_table(DATASET_DIR, "ARMAS")
    apmas = eds.open_table(DATASET_DIR, "APMAS")

    sales_entries = eds.build_sales_entries(artrn, stcrd, armas)
    purchase_entries = eds.build_purchase_entries(aptrn, stcrd, apmas)

    s_start, s_end = SALES_WINDOW
    dbf_sales = defaultdict(float)
    for e in sales_entries:
        if s_start <= e["date_iso"] <= s_end:
            dbf_sales[e["doc_no"].rsplit("-", 1)[0]] += e["net"]

    p_start, p_end = PURCHASE_WINDOW
    dbf_purch = defaultdict(float)
    for e in purchase_entries:
        if p_start <= e["date_iso"] <= p_end:
            dbf_purch[e["doc_no"]] += e["net"]

    conn = sendy_conn()
    try:
        sendy_sales = defaultdict(float)
        for row in conn.execute(
            "SELECT doc_base, SUM(net) n FROM sales_transactions "
            "WHERE date_iso BETWEEN ? AND ? GROUP BY doc_base", (s_start, s_end)
        ):
            sendy_sales[row[0]] = row[1]

        sendy_purch = defaultdict(float)
        for row in conn.execute(
            "SELECT doc_base, SUM(net) n FROM purchase_transactions "
            "WHERE date_iso BETWEEN ? AND ? GROUP BY doc_base", (p_start, p_end)
        ):
            sendy_purch[row[0]] = row[1]
    finally:
        conn.close()

    sales_gap = three_way(dbf_sales, sendy_sales, f"sales {s_start}..{s_end}")
    purch_gap = three_way(dbf_purch, sendy_purch, f"purchase {p_start}..{p_end}")

    if sales_gap or purch_gap:
        print("\nFAIL: Sendy-only gap > 0 — adapter misses a doc class Sendy books.")
        return 1
    print("\nOK: Sendy-only = 0 for both types (see MAPPING.md for known matched-doc "
          "mismatches root-caused as pre-existing text-import drift, not adapter bugs).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
