"""
DEPRECATED: one-off from 2026-04-28. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Idempotent re-import of BSN credit notes (ใบลดหนี้ / SR rows) into sales_transactions.

Source:
  /Volumes/ZYRINGE/express_data/ใบลดหนี้-27.4.69.csv  (cp874, 194 SR masters / 252 details)

Goal:
  UPSERT one sales_transactions row per detail line. SR rows live in the same
  table as IV rows (sales_transactions) — distinguished only by doc_no LIKE 'SR%'.

Logic mirrors models.import_weekly()'s non-weekly branch:
  - Match key: (bsn_code, doc_no OR doc_base)  →  delete matching, then insert.
  - Resolve product_id via product_code_mapping by bsn_code.
  - Track new bsn_codes → INSERT OR IGNORE into product_code_mapping.
  - All inserted rows: synced_to_stock=1.  SR rows do NOT trigger stock adjustments
    here — Phase 1 (run.py) already rebuilt the stock ledger so SR effects are baked
    into the back-solved baseline. Re-importing SR data must not double-count.

Run:
  python import_credit_notes.py            # dry-run (rollback at end, DEFAULT)
  python import_credit_notes.py --commit   # commit changes

Decisions:
  * customer_code: source CSV has no code, only customer name. We attempt an
    exact-match lookup against `customers.name`; falls back to first LIKE match
    if no exact hit; NULL otherwise. ~10-15% of credit-note customers are
    "หน้าร้านS/L/B" (walk-ins) — these never resolve to a real code.
  * ref_invoice / ref_invoice_line: schema has no field. Dropped. We log how many
    detail rows had a ref_invoice_line so the user can decide later if a comment
    column is worth adding.
  * Cancelled (*SR…): inserted as normal rows. Cancellation flag is dropped (no
    schema field). 3 rows total in source — flagged in the report.
  * Placeholder masters (no detail rows, 9 total): ONE row inserted per master
    with bsn_code=NULL, qty=0, total=0. Preserves doc_no traceability. The
    placeholder will fail the UNIQUE-ish duplicate guard (since bsn_code is
    NULL, the (bsn_code, doc_no) match returns nothing), so re-runs will
    accumulate duplicates — caller responsibility to run only once per import.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ERP_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ERP_ROOT / "inventory_app"
sys.path.insert(0, str(APP_DIR))

from parse_weekly import parse_credit_notes  # noqa: E402

DB_PATH = str(APP_DIR / "instance" / "inventory.db")
SOURCE_FILE = "/Volumes/ZYRINGE/express_data/ใบลดหนี้-27.4.69.csv"


# ── Snapshot helpers ─────────────────────────────────────────────────────────

def snapshot(conn, label):
    return {
        "label": label,
        "sales_rows_total": conn.execute(
            "SELECT COUNT(*) FROM sales_transactions"
        ).fetchone()[0],
        "sales_rows_sr": conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no LIKE 'SR%'"
        ).fetchone()[0],
        "sales_rows_sr_distinct_doc_base": conn.execute(
            "SELECT COUNT(DISTINCT doc_base) FROM sales_transactions"
            " WHERE doc_no LIKE 'SR%'"
        ).fetchone()[0],
        "product_code_mapping_rows": conn.execute(
            "SELECT COUNT(*) FROM product_code_mapping"
        ).fetchone()[0],
    }


def lookup_customer_code(conn, name):
    """Try exact match, then LIKE. Returns code or None."""
    if not name:
        return None
    row = conn.execute(
        "SELECT code FROM customers WHERE name = ?", (name,)
    ).fetchone()
    if row:
        return row["code"]
    row = conn.execute(
        "SELECT code FROM customers WHERE name LIKE ? LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return row["code"] if row else None


# ── Upsert ───────────────────────────────────────────────────────────────────

def upsert_credit_notes(conn, entries):
    """
    Mirror models.import_weekly()'s non-weekly branch (doc_no contains '-').

    Match:  (bsn_code, doc_no OR doc_base)
    Action: DELETE matching → INSERT new with synced_to_stock=1
    Resolve product_id via product_code_mapping
    """
    cur = conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
        " VALUES (?, 0, 0, ?)",
        ("reimport_2026_04_28_credit_notes", "reimport-credit-notes"),
    )
    batch_id = cur.lastrowid

    inserted = overwritten_rows = skipped_ignored = 0
    placeholder_inserted = cancelled_inserted = 0
    customer_code_resolved = customer_code_null = 0
    new_bsn_codes = {}
    ref_line_count = 0
    customer_cache = {}  # avoid duplicate lookups

    for e in entries:
        doc_no = e["doc_no"]
        doc_base = e["doc_base"]
        bsn_code = e["bsn_code"]
        is_placeholder = bsn_code is None

        if not is_placeholder:
            old = conn.execute(
                "SELECT id FROM sales_transactions"
                " WHERE bsn_code = ? AND (doc_no = ? OR doc_no = ?)",
                (bsn_code, doc_no, doc_base),
            ).fetchall()
            if old:
                ids = [r["id"] for r in old]
                placeholders = ",".join(["?"] * len(ids))
                conn.execute(
                    f"DELETE FROM sales_transactions WHERE id IN ({placeholders})",
                    ids,
                )
                overwritten_rows += len(ids)
        else:
            # Placeholder: match by doc_no with NULL bsn_code (re-run guard)
            old = conn.execute(
                "SELECT id FROM sales_transactions"
                " WHERE doc_no = ? AND bsn_code IS NULL",
                (doc_no,),
            ).fetchall()
            if old:
                ids = [r["id"] for r in old]
                placeholders = ",".join(["?"] * len(ids))
                conn.execute(
                    f"DELETE FROM sales_transactions WHERE id IN ({placeholders})",
                    ids,
                )
                overwritten_rows += len(ids)

        # Resolve product_id
        product_id = None
        is_ignored = 0
        if bsn_code:
            mapping = conn.execute(
                "SELECT product_id, is_ignored FROM product_code_mapping"
                " WHERE bsn_code = ?",
                (bsn_code,),
            ).fetchone()
            if mapping:
                product_id = mapping["product_id"]
                is_ignored = mapping["is_ignored"]

        if is_ignored:
            skipped_ignored += 1
            continue

        if bsn_code and not product_id:
            new_bsn_codes[bsn_code] = e["product_name_raw"]

        # Resolve customer_code
        cust_name = e["customer"]
        if cust_name not in customer_cache:
            customer_cache[cust_name] = lookup_customer_code(conn, cust_name)
        cust_code = customer_cache[cust_name]
        if cust_code:
            customer_code_resolved += 1
        else:
            customer_code_null += 1

        if e["ref_invoice_line"]:
            ref_line_count += 1
        if e["cancelled"]:
            cancelled_inserted += 1
        if is_placeholder:
            placeholder_inserted += 1

        # ref_invoice column populated from master row (the IV being credited).
        # ref_invoice_line (e.g. "IV6602028-3") points to the specific original
        # sales line — only the master IV is stored; line precision dropped.
        conn.execute(
            """
            INSERT INTO sales_transactions
                (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
                 product_name_raw, customer, customer_code, qty, unit, unit_price,
                 vat_type, discount, total, net, ref_invoice, synced_to_stock)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (
                batch_id, e["date_iso"], doc_no, doc_base, product_id, bsn_code,
                e["product_name_raw"], cust_name, cust_code,
                e["qty"], e["unit"], e["unit_price"],
                e["vat_type"], e["discount"], e["total"], e["net"],
                e["ref_invoice"],
            ),
        )
        inserted += 1

    new_codes_inserted = 0
    for code, name in new_bsn_codes.items():
        cur2 = conn.execute(
            "INSERT OR IGNORE INTO product_code_mapping (bsn_code, bsn_name)"
            " VALUES (?, ?)",
            (code, name),
        )
        if cur2.rowcount:
            new_codes_inserted += 1

    conn.execute(
        "UPDATE import_log SET rows_imported = ?, rows_skipped = ? WHERE id = ?",
        (inserted, skipped_ignored, batch_id),
    )

    return {
        "parsed": len(entries),
        "inserted": inserted,
        "overwritten_old_rows": overwritten_rows,
        "skipped_ignored": skipped_ignored,
        "placeholder_inserted": placeholder_inserted,
        "cancelled_inserted": cancelled_inserted,
        "customer_code_resolved": customer_code_resolved,
        "customer_code_null": customer_code_null,
        "ref_invoice_line_dropped": ref_line_count,
        "new_bsn_codes_total": len(new_bsn_codes),
        "new_bsn_codes_inserted": new_codes_inserted,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Commit changes (default: dry-run, rollback at end)")
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY-RUN (rollback at end)"
    print(f"=== Credit-Note Re-import [{mode}] ===")
    print(f"DB:     {DB_PATH}")
    print(f"Source: {SOURCE_FILE}")
    print()

    # Parse first (independent of DB)
    entries = parse_credit_notes(SOURCE_FILE)
    print(f"Parsed {len(entries):,} entries")
    print(f"  distinct SR docs:       {len({e['doc_base'] for e in entries}):,}")
    print(f"  placeholder (no detail): {sum(1 for e in entries if e['bsn_code'] is None):,}")
    print(f"  cancelled (*SR):         {sum(1 for e in entries if e['cancelled']):,}")

    yc = Counter(e["date_iso"][:4] for e in entries)
    print(f"  by year:                {dict(yc)}")

    vc = Counter(e["vat_type"] for e in entries)
    print(f"  by vat_type:            {dict(vc)}")
    print()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        before = snapshot(conn, "before")
        print("BEFORE:")
        print(json.dumps(before, indent=2, default=str))
        print()

        metrics = upsert_credit_notes(conn, entries)
        print("UPSERT METRICS:")
        print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))
        print()

        after = snapshot(conn, "after")
        print("AFTER:")
        print(json.dumps(after, indent=2, default=str))
        print()

        print("─── DIFF (after - before) ───")
        for k in before:
            if k == "label":
                continue
            b, a = before[k], after[k]
            if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                diff = a - b
                arrow = "" if diff == 0 else (" UP" if diff > 0 else " DOWN")
                print(f"  {k:42s}  {b:>10,}  ->  {a:>10,}   ({diff:+,}){arrow}")
        print()

        # Sample first/last few SR rows that would be in DB after upsert
        print("─── Sample inserted SR rows (first 3) ───")
        rows = conn.execute(
            "SELECT date_iso, doc_no, bsn_code, customer, customer_code,"
            "       qty, unit, unit_price, total, vat_type"
            " FROM sales_transactions"
            " WHERE doc_no LIKE 'SR%'"
            "   AND batch_id = (SELECT MAX(batch_id) FROM sales_transactions WHERE doc_no LIKE 'SR%')"
            " ORDER BY date_iso, doc_no LIMIT 3"
        ).fetchall()
        for r in rows:
            print(f"  {dict(r)}")
        print()

        if args.commit:
            conn.commit()
            print("COMMITTED")
        else:
            conn.rollback()
            print("ROLLED BACK (dry-run)")
    except Exception:
        conn.rollback()
        print("EXCEPTION — rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
