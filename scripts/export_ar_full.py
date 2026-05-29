#!/usr/bin/env python3
"""Export the full BSN AR open-invoice register to CSV.

Source: express_ar_outstanding WHERE entity='BSN' at the latest snapshot.
Express is the authoritative source for BSN AR as of 2026-05-29.

Columns (one row per open invoice):
  customer, cust_code, salesperson, doc_no, inv_date, age_days,
  payable, paid, outstanding

Definitions:
  payable     = bill_amount   (gross billed)
  paid        = paid_amount   (amount collected per Express)
  outstanding = outstanding_amount (what the customer still owes)
  age_days    = days from doc_date_iso to the snapshot date (point-in-time)

Pre-write assertion:
  total outstanding == 1,299,335.94
  doc count == 200
  customer count == 72

Usage:
  /Users/putty/.virtualenvs/erp/bin/python sendy_erp/scripts/export_ar_full.py [out.csv]
Default out path: Operations/05_analysis-reports/finance/ar_full_2026-05-29.csv
"""
import csv
import os
import sqlite3
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))               # sendy_erp/scripts
_APP = os.path.join(_HERE, "..", "inventory_app")
_WORKSPACE = os.path.normpath(os.path.join(_HERE, "..", ".."))   # ~/Sendai-Boonsawat
sys.path.insert(0, _APP)

DB_PATH = os.path.normpath(os.path.join(_APP, "instance", "inventory.db"))

EXPECTED_TOTAL = 1299335.94
EXPECTED_DOCS = 200
EXPECTED_CUSTOMERS = 72


def build_rows(conn):
    row = conn.execute(
        "SELECT MAX(snapshot_date_iso) AS snap FROM express_ar_outstanding"
        " WHERE entity='BSN'"
    ).fetchone()
    snap = row["snap"]
    if not snap:
        raise RuntimeError("No BSN snapshot found in express_ar_outstanding")
    snap_date = date.fromisoformat(snap)

    rows = conn.execute("""
        SELECT
            ao.customer_code,
            COALESCE(c.name, ao.customer_name)  AS customer_name,
            ao.salesperson_code,
            ao.doc_no,
            ao.doc_date_iso,
            ao.bill_amount,
            ao.paid_amount,
            ao.outstanding_amount
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.entity = 'BSN'
          AND ao.snapshot_date_iso = ?
        ORDER BY ao.outstanding_amount DESC
    """, (snap,)).fetchall()

    out = []
    for r in rows:
        age = None
        if r["doc_date_iso"]:
            try:
                age = (snap_date - date.fromisoformat(r["doc_date_iso"])).days
                age = max(age, 0)
            except (ValueError, TypeError):
                age = None
        out.append({
            "customer": r["customer_name"] or "",
            "cust_code": r["customer_code"] or "",
            "salesperson": r["salesperson_code"] or "",
            "doc_no": r["doc_no"],
            "inv_date": r["doc_date_iso"] or "",
            "age_days": age if age is not None else "",
            "payable": round(float(r["bill_amount"] or 0), 2),
            "paid": round(float(r["paid_amount"] or 0), 2),
            "outstanding": round(float(r["outstanding_amount"] or 0), 2),
        })
    return out


FIELDS = ["customer", "cust_code", "salesperson", "doc_no", "inv_date",
          "age_days", "payable", "paid", "outstanding"]


def main():
    out_path = (sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _WORKSPACE, "Operations", "05_analysis-reports", "finance",
        "ar_full_2026-05-29.csv"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = build_rows(conn)
    conn.close()

    # Assertions before writing — guard against importing wrong snapshot.
    tot = round(sum(r["outstanding"] for r in rows), 2)
    doc_count = len(rows)
    cust_count = len({r["cust_code"] for r in rows if r["cust_code"]})

    assert doc_count == EXPECTED_DOCS, \
        f"Expected {EXPECTED_DOCS} docs, got {doc_count}"
    assert cust_count == EXPECTED_CUSTOMERS, \
        f"Expected {EXPECTED_CUSTOMERS} customers, got {cust_count}"
    assert abs(tot - EXPECTED_TOTAL) < 0.02, \
        f"Expected total ฿{EXPECTED_TOTAL:,.2f}, got ฿{tot:,.2f}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {doc_count} rows → {out_path}")
    print(f"docs       = {doc_count}  (expected {EXPECTED_DOCS})")
    print(f"customers  = {cust_count}  (expected {EXPECTED_CUSTOMERS})")
    print(f"outstanding = ฿{tot:,.2f}  (expected ฿{EXPECTED_TOTAL:,.2f})")


if __name__ == "__main__":
    main()
