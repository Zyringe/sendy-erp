"""2026-09-30 — re-scan ตรวจบิล docs whose flags point at deleted lines (#681).

WHY. The Express DBF upload replaced sales lines without re-scanning them, so
/review kept flags on `txn_id`s that no longer exist. The route now re-scans
(#681); this clears what piled up before it did. Measured on prod 2026-09-30:
26 flags on 21 docs.

Writes txn_review_docs / txn_review_flags only, through review_rules.scan_docs,
the same call the import hooks make. Both tables are derived: `scan_all` rebuilds
them from sales_transactions, so there is no backup step.

    python3 scripts/2026_09_30_rescan_orphaned_review_flags_681.py --db /data/inventory.db
    python3 scripts/2026_09_30_rescan_orphaned_review_flags_681.py --db /data/inventory.db --apply
"""
import argparse
import os
import sqlite3
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'inventory_app'))

ORPHAN_DOCS = """
    SELECT DISTINCT f.doc_base FROM txn_review_flags f
    WHERE f.txn_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM sales_transactions s WHERE s.id = f.txn_id)
    ORDER BY f.doc_base
"""


def _connect(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    conn = _connect(args.db)
    docs = [r['doc_base'] for r in conn.execute(ORPHAN_DOCS)]
    print(f'{len(docs)} docs with orphaned flags: {", ".join(docs)}')
    if not docs or not args.apply:
        return 0

    import review_rules as rr
    conn.execute('BEGIN IMMEDIATE')
    try:
        summary = rr.scan_docs(docs, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f'scan_docs: {summary}')

    left = [r['doc_base'] for r in _connect(args.db).execute(ORPHAN_DOCS)]
    if left:
        print(f'FAIL: still orphaned after re-scan: {left}')
        return 1
    print('OK: 0 docs with orphaned flags')
    return 0


if __name__ == '__main__':
    sys.exit(main())
