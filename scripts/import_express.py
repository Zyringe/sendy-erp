"""Import a parsed Express file into the express_* tables.

One CLI per session: pick the file_type explicitly, point at the
exported CSV, the script parses + inserts atomically inside one
batch row in `express_import_log`. If anything raises, the whole
batch rolls back.

Usage:
    python scripts/import_express.py credit_notes  /path/to/ใบลดหนี้.csv
    python scripts/import_express.py payments_in   /path/to/การรับชำระหนี้.csv
    python scripts/import_express.py ar_snapshot   /path/to/ลูกหนี้คงค้าง.csv
    python scripts/import_express.py payments_out  /path/to/จ่ายชำระหนี้.csv
    python scripts/import_express.py sales         /path/to/ขาย.csv

Add --dry-run to parse-only without writing to DB. Add --company SD
to attribute the batch to Sendai Trading instead of BSN (default).

Lookup backfill (customer_id, supplier_id) runs best-effort by exact
name match. Anything that doesn't match stays NULL — mapping work
happens separately, not blocking import.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

# Resolve sibling modules
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / 'inventory_app'))  # for bsn_units

import parse_express_credit_notes as p_cn        # noqa: E402
import parse_express_payments_in as p_pin        # noqa: E402
import parse_express_ar_snapshot as p_ar          # noqa: E402
import parse_express_ap_snapshot as p_ap          # noqa: E402
import parse_express_payments_out as p_pout       # noqa: E402
import parse_express_sales as p_sales             # noqa: E402
import bsn_units                                   # noqa: E402

DB_PATH = _HERE.parent / 'inventory_app' / 'instance' / 'inventory.db'


# ── small lookup helpers ─────────────────────────────────────────────────────
def _company_id(conn, code):
    row = conn.execute('SELECT id FROM companies WHERE code = ?', (code,)).fetchone()
    if row is None:
        raise SystemExit(f'No company row for code={code!r}. Has migration 011 run?')
    return row[0]


def _supplier_id_by_name(conn, name):
    """Best-effort exact-name match against suppliers.name."""
    if not name:
        return None
    row = conn.execute('SELECT id FROM suppliers WHERE name = ?', (name.strip(),)).fetchone()
    return row[0] if row else None


def _customer_code_by_name(conn, name):
    """Best-effort exact-name match against customers.name."""
    if not name:
        return None
    row = conn.execute('SELECT code FROM customers WHERE name = ?', (name.strip(),)).fetchone()
    return row[0] if row else None


def _product_id_by_code(conn, code, unit=None):
    """Resolve Express product_code (+ unit) → Sendy product_id via
    product_code_mapping. Uses the canonical unit-aware predicate (mirrors
    mig 063/064 resolver): exact (bsn_code, bsn_unit) beats bsn_unit='' catch-all.
    Returns None for unmapped codes.
    """
    if not code:
        return None
    row = conn.execute(
        """
        SELECT m.product_id
          FROM product_code_mapping m
         WHERE m.bsn_code = ?
           AND m.bsn_unit IN (COALESCE(?, ''), '')
           AND m.product_id IS NOT NULL
         ORDER BY (m.bsn_unit = '')
         LIMIT 1
        """,
        (code, unit),
    ).fetchone()
    return row[0] if row else None


# ── per-file-type writers ────────────────────────────────────────────────────
def _existing_doc_nos(conn, table):
    return {r[0] for r in conn.execute(f'SELECT doc_no FROM {table}').fetchall()}


def _import_credit_notes(conn, path, batch_id, company_id, incremental=True):
    records = list(p_cn.parse_credit_notes(path))
    skip = _existing_doc_nos(conn, 'express_credit_notes') if incremental else set()
    skipped = 0
    line_count = 0
    for r in records:
        if r.doc_no in skip:
            skipped += 1
            continue
        cur = conn.execute("""
            INSERT INTO express_credit_notes
                (batch_id, doc_no, date_iso, company_id, supplier_name, supplier_id,
                 ref_doc, discount_amount, vat_amount, total_amount,
                 is_cleared, is_void, type_code, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id, r.doc_no, r.date_iso, company_id, r.supplier_name,
            _supplier_id_by_name(conn, r.supplier_name),
            r.ref_doc, r.discount, r.vat, r.total,
            int(r.is_cleared), int(r.is_void), r.type_code, r.note,
        ))
        cn_id = cur.lastrowid
        for ln in r.lines:
            conn.execute("""
                INSERT INTO express_credit_note_lines
                    (credit_note_id, line_no, product_code, product_id,
                     product_name_raw, qty, unit, unit_price,
                     discount, line_total, is_cleared)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cn_id, ln.line_no, ln.product_code,
                _product_id_by_code(conn, ln.product_code),
                ln.product_name, ln.qty, ln.unit, ln.unit_price,
                ln.discount, ln.line_total, int(ln.is_cleared),
            ))
            line_count += 1
    return len(records) - skipped, line_count


def _import_payments_in(conn, path, batch_id, company_id, incremental=True):
    records = list(p_pin.parse_payments_in(path))
    skip = _existing_doc_nos(conn, 'express_payments_in') if incremental else set()
    skipped = 0
    line_count = 0
    for r in records:
        if r.doc_no in skip:
            skipped += 1
            continue
        customer_code = _customer_code_by_name(conn, r.customer_name)
        cur = conn.execute("""
            INSERT INTO express_payments_in
                (batch_id, doc_no, date_iso, company_id,
                 customer_name, customer_code, customer_id, salesperson_code, is_void,
                 deposit_applied, invoice_amount, cash_amount, cheque_amount,
                 interest_amount, discount_amount, vat_amount,
                 cheque_no, cheque_date_iso, bank, cheque_status, note)
            VALUES (?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?)
        """, (
            batch_id, r.doc_no, r.date_iso, company_id,
            r.customer_name, customer_code, customer_code,
            r.salesperson_code, int(r.is_void),
            r.deposit_applied, r.invoice_amount, r.cash_amount, r.cheque_amount,
            r.interest_amount, r.discount_amount, r.vat_amount,
            r.cheque_no, r.cheque_date_iso, r.bank, r.cheque_status, r.note,
        ))
        pid = cur.lastrowid
        for ref in r.invoice_refs:
            conn.execute("""
                INSERT INTO express_payment_in_invoice_refs
                    (payment_in_id, invoice_no, invoice_date_iso, amount)
                VALUES (?, ?, ?, ?)
            """, (pid, ref.invoice_no, ref.invoice_date_iso, ref.amount))
            line_count += 1
    return len(records) - skipped, line_count


def _import_ar_snapshot(conn, path, batch_id, company_id, incremental=True,
                        entity='SD'):
    """ar_snapshot is always a full snapshot — incremental flag ignored.
    Each call inserts the latest snapshot under a new batch (the engine
    queries MAX(snapshot_date_iso) per entity so old batches are naturally
    superseded for display).

    entity: 'SD' (Sendai Trading, default) or 'BSN' (Boonsawat).
    """
    records = list(p_ar.parse_ar_snapshot(path))
    # Reconcile parsed rows to the report footer BEFORE writing — a format drift
    # that drops detail rows must abort, not become authoritative. (run_import
    # wraps this in a transaction that rolls back on any exception.)
    p_ar.validate(records, path)
    # Snapshot date = the report's "as of" header date (true คงค้าง date). Fall
    # back to the latest doc_date_iso only if the header can't be parsed.
    snapshot_date = (p_ar.report_asof_date(path)
                     or max((r.doc_date_iso for r in records if r.doc_date_iso), default=''))
    # Idempotent: replace any prior rows for the same (entity, snapshot_date) so
    # re-uploading the same report can't double-count (reader sums MAX-date rows).
    conn.execute("DELETE FROM express_ar_outstanding WHERE entity = ? AND snapshot_date_iso = ?",
                 (entity, snapshot_date))
    for r in records:
        customer_code = (r.customer_code or '').strip()
        # Some customer headers carry codes that don't match customers.code (legacy);
        # try direct match first, then by name.
        cust_id = None
        if customer_code:
            row = conn.execute('SELECT code FROM customers WHERE code = ?', (customer_code,)).fetchone()
            cust_id = row[0] if row else _customer_code_by_name(conn, r.customer_name)
        else:
            cust_id = _customer_code_by_name(conn, r.customer_name)
        conn.execute("""
            INSERT INTO express_ar_outstanding
                (batch_id, entity, snapshot_date_iso, customer_code, customer_name,
                 customer_id, customer_type, doc_date_iso, doc_no, is_anomalous,
                 salesperson_code, bill_amount, paid_amount, outstanding_amount, has_warning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id, entity, snapshot_date, customer_code, r.customer_name, cust_id,
            r.customer_type, r.doc_date_iso, r.doc_no, int(r.is_anomalous),
            r.salesperson_code, r.bill_amount, r.paid_amount, r.outstanding_amount,
            int(r.has_warning),
        ))
    # Also stamp snapshot_date back onto the batch row for visibility
    conn.execute('UPDATE express_import_log SET snapshot_date_iso = ? WHERE id = ?',
                 (snapshot_date, batch_id))
    return len(records), 0


def _import_ap_snapshot(conn, path, batch_id, company_id, incremental=True,
                        entity='BSN'):
    """ap_snapshot is always a full snapshot — incremental flag ignored.
    Each call inserts under a new batch; MAX(snapshot_date_iso) per entity
    determines the current snapshot for display.

    entity: 'BSN' (default, Boonsawat).
    """
    records, grand_total, subtotals = p_ap.parse_ap_snapshot(path)
    # Reconcile parsed rows to the report footer + per-supplier subtotals BEFORE
    # writing — abort on any format-drift mismatch (run_import rolls back).
    p_ap._validate(records, grand_total, subtotals)
    snapshot_date = (p_ap.report_asof_date(path)
                     or max((r.doc_date_iso for r in records if r.doc_date_iso), default=''))
    # Idempotent: replace any prior rows for the same (entity, snapshot_date).
    conn.execute("DELETE FROM express_ap_outstanding WHERE entity = ? AND snapshot_date_iso = ?",
                 (entity, snapshot_date))
    for r in records:
        supplier_id = _supplier_id_by_name(conn, r.supplier_name)
        conn.execute("""
            INSERT INTO express_ap_outstanding
                (batch_id, entity, snapshot_date_iso, supplier_type, supplier_name,
                 supplier_code, supplier_id, doc_no, supplier_invoice_no,
                 doc_date_iso, bill_amount, paid_amount, outstanding_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id, entity, snapshot_date, r.supplier_type, r.supplier_name,
            r.supplier_code, supplier_id, r.doc_no, r.supplier_invoice_no,
            r.doc_date_iso, r.bill_amount, r.paid_amount, r.outstanding_amount,
        ))
    conn.execute('UPDATE express_import_log SET snapshot_date_iso = ? WHERE id = ?',
                 (snapshot_date, batch_id))
    return len(records), 0


def _import_payments_out(conn, path, batch_id, company_id, incremental=True):
    records = list(p_pout.parse_payments_out(path))
    skip = _existing_doc_nos(conn, 'express_payments_out') if incremental else set()
    skipped = 0
    line_count = 0
    for r in records:
        if r.doc_no in skip:
            skipped += 1
            continue
        cur = conn.execute("""
            INSERT INTO express_payments_out
                (batch_id, doc_no, date_iso, company_id,
                 supplier_name, supplier_id, is_void,
                 deposit_applied, invoice_amount, cash_amount, cheque_amount,
                 interest_amount, discount_amount, vat_amount,
                 cheque_no, cheque_date_iso, bank, cheque_status, note)
            VALUES (?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?)
        """, (
            batch_id, r.doc_no, r.date_iso, company_id,
            r.supplier_name, _supplier_id_by_name(conn, r.supplier_name), int(r.is_void),
            r.deposit_applied, r.invoice_amount, r.cash_amount, r.cheque_amount,
            r.interest_amount, r.discount_amount, r.vat_amount,
            r.cheque_no, r.cheque_date_iso, r.bank, r.cheque_status, r.note,
        ))
        pid = cur.lastrowid
        for ref in r.receive_refs:
            conn.execute("""
                INSERT INTO express_payment_out_receive_refs
                    (payment_out_id, receive_doc, receive_date_iso, invoice_ref, amount)
                VALUES (?, ?, ?, ?, ?)
            """, (pid, ref.receive_doc, ref.receive_date_iso, ref.invoice_ref, ref.amount))
            line_count += 1
    return len(records) - skipped, line_count


def _import_sales(conn, path, batch_id, company_id, incremental=True):
    records = list(p_sales.parse_sales(path))
    # sales: dedupe by (doc_no, line_no) since one invoice has many lines
    skip = set()
    if incremental:
        skip = {(r[0], r[1]) for r in conn.execute(
            'SELECT doc_no, line_no FROM express_sales').fetchall()}
    skipped = 0
    for r in records:
        if (r.doc_no, r.line_no) in skip:
            skipped += 1
            continue
        # Doc-type prefix from doc_no
        doc_type = r.doc_no[:2]
        customer_code = (r.customer_code or '').strip()
        cust_id = None
        if customer_code:
            row = conn.execute('SELECT code FROM customers WHERE code = ?', (customer_code,)).fetchone()
            cust_id = row[0] if row else None
        # Normalize unit FIRST — both the mapping resolver and the canonical
        # express_sales.unit value must be on the canonical alias.
        norm_unit = bsn_units.normalize_unit(r.unit)
        prod_id = _product_id_by_code(conn, r.product_code, norm_unit)
        conn.execute("""
            INSERT INTO express_sales
                (batch_id, doc_no, line_no, doc_type, date_iso, company_id,
                 customer_code, customer_name, customer_id,
                 product_code, product_id, product_name_raw,
                 qty, unit, return_flag, unit_price, vat_type,
                 discount, total, total_discount, net, ref_doc, is_warning)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
        """, (
            batch_id, r.doc_no, r.line_no, doc_type, r.date_iso, company_id,
            customer_code, r.customer_name, cust_id,
            r.product_code, prod_id, r.product_name,
            r.qty, norm_unit, r.return_flag, r.unit_price, r.vat_type,
            r.discount, r.total, r.total_discount, r.net, r.ref_doc, int(r.is_warning),
        ))
    return len(records) - skipped, 0


_IMPORTERS = {
    'credit_notes':  _import_credit_notes,
    'payments_in':   _import_payments_in,
    'ar_snapshot':   _import_ar_snapshot,
    'ap_snapshot':   _import_ap_snapshot,
    'payments_out':  _import_payments_out,
    'sales':         _import_sales,
}


# ── orchestration ────────────────────────────────────────────────────────────

# Snapshot importers receive an explicit entity tag (company_code used directly).
# For ar_snapshot the legacy default is 'SD'; for ap_snapshot it is 'BSN'.
_SNAPSHOT_IMPORTERS = {'ar_snapshot', 'ap_snapshot'}


def run_import(file_type, path, company_code='BSN', dry_run=False, incremental=True,
               db_path=None):
    if file_type not in _IMPORTERS:
        raise SystemExit(f'unknown file_type {file_type!r} — pick from {sorted(_IMPORTERS)}')

    # db_path lets the web app inject config.DATABASE_PATH (which honours DATA_DIR,
    # e.g. /data/inventory.db on Railway). The module-level DB_PATH is only a
    # convenience default for local CLI runs — on prod it points at a directory
    # that doesn't exist, so relying on it raised "unable to open database file".
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute('PRAGMA foreign_keys = OFF')   # match app behaviour
    company_id = _company_id(conn, company_code)

    if dry_run:
        # Parse-only: bypass DB writes
        if file_type == 'credit_notes':
            records = list(p_cn.parse_credit_notes(path))
            print(f'[dry-run] credit_notes: {len(records)} records')
        elif file_type == 'payments_in':
            records = list(p_pin.parse_payments_in(path))
            print(f'[dry-run] payments_in: {len(records)} records')
        elif file_type == 'ar_snapshot':
            records = list(p_ar.parse_ar_snapshot(path))
            print(f'[dry-run] ar_snapshot: {len(records)} records')
        elif file_type == 'ap_snapshot':
            records, _, _ = p_ap.parse_ap_snapshot(path)
            print(f'[dry-run] ap_snapshot: {len(records)} records')
        elif file_type == 'payments_out':
            records = list(p_pout.parse_payments_out(path))
            print(f'[dry-run] payments_out: {len(records)} records')
        elif file_type == 'sales':
            records = list(p_sales.parse_sales(path))
            print(f'[dry-run] sales: {len(records)} records')
        conn.close()
        return

    cur = conn.execute("""
        INSERT INTO express_import_log
            (file_type, source_filename, company_id, status, note)
        VALUES (?, ?, ?, 'imported', ?)
    """, (file_type, str(path), company_id, f'imported via scripts/import_express.py'))
    batch_id = cur.lastrowid

    try:
        if file_type in _SNAPSHOT_IMPORTERS:
            record_count, line_count = _IMPORTERS[file_type](
                conn, path, batch_id, company_id,
                incremental=incremental, entity=company_code)
        else:
            record_count, line_count = _IMPORTERS[file_type](
                conn, path, batch_id, company_id, incremental=incremental)
        conn.execute("""
            UPDATE express_import_log
            SET record_count = ?, line_count = ?
            WHERE id = ?
        """, (record_count, line_count, batch_id))
        conn.commit()
        print(f'OK batch_id={batch_id}  records={record_count}  lines={line_count}')
    except Exception as exc:
        conn.rollback()
        print(f'FAILED batch — rolled back. error: {exc}', file=sys.stderr)
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file_type', choices=sorted(_IMPORTERS))
    ap.add_argument('path', type=Path)
    ap.add_argument('--company', default='BSN', help='company code (BSN or SD)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--full', action='store_true',
                    help='disable incremental dedup (re-imports duplicate doc_no)')
    args = ap.parse_args()
    run_import(args.file_type, args.path, args.company, args.dry_run,
               incremental=not args.full)


if __name__ == '__main__':
    main()
