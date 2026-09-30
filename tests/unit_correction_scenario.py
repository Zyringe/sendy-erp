"""Shared fixture builders for the line unit correction tests (#692).

Not a test module. Everything is built on `empty_db` through the real
importers, so a sales line always has the ledger row `_sync_bsn_to_stock`
would have written for it. `empty_db` carries no unit map rows, so every
spelling is its own word until a test adds a row.
"""
import datetime
import os
import sqlite3
import sys

REASON = 'ลูกค้าซื้อเป็นหลอด คีย์ผิดเป็นโหล'
SALE_DAY = datetime.date(2026, 4, 1)
CODE = '031บ9500'


def conn(path):
    import database
    assert database.DATABASE_PATH == path
    return database.get_connection()


def raw(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def seed_product(path, code=CODE, *, unit_type='หลอด', ratios=(('โหล', 12),),
                 name='ใบมีดคัตเตอร์'):
    c = conn(path)
    pid = c.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
        (name, unit_type)).lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
              (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)"
              " VALUES (?, ?, ?)", (code, name, pid))
    for unit, ratio in ratios:
        c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                  " VALUES (?, ?, ?)", (pid, unit, ratio))
    c.commit()
    c.close()
    return pid


def seed_company(path):
    c = raw(path)
    c.execute("INSERT INTO companies (code, name_th) VALUES ('BSN', 'บริษัท ทดสอบ จำกัด')")
    c.commit()
    c.close()


class Book:
    """Fake Express tables, in the shape `express_dbf_source.open_table` returns."""

    def __init__(self):
        self.artrn, self.aptrn, self.stcrd = [], [], []

    def add_line(self, doc, seq, code, qty, unit, price, net=None):
        value = round(qty * price, 2) if net is None else net
        self.stcrd.append({
            'DOCNUM': doc, 'SEQNUM': seq, 'STKCOD': code, 'STKDES': 'name',
            'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': price, 'DISC': '',
            'TRNVAL': value, 'NETVAL': value, 'RDOCNUM': ''})

    def sale(self, doc, lines, day=SALE_DAY):
        """lines: (seq, code, qty, unit, price[, net]). An SR doc is a return."""
        import document_kind
        if document_kind.is_return(doc, 'sales'):
            self.artrn.append({'DOCNUM': doc, 'RECTYP': '5', 'CUSCOD': 'C001',
                               'SONUM': None, 'TOTAL': 0.0, 'FLGVAT': 0, 'DOCDAT': day})
        else:
            self.artrn.append({'DOCNUM': doc, 'RECTYP': '3', 'CUSCOD': 'C001',
                               'FLGVAT': 0, 'DOCDAT': day, 'YOUREF': None})
        for seq, code, qty, unit, price, *net in lines:
            self.add_line(doc, seq, code, qty, unit, price, *net)
        return self

    def purchase(self, doc, lines, day):
        self.aptrn.append({'DOCNUM': doc, 'RECTYP': '3', 'SUPCOD': 'S001',
                           'FLGVAT': 0, 'DOCDAT': day})
        for seq, code, qty, unit, price, *net in lines:
            self.add_line(doc, seq, code, qty, unit, price, *net)
        return self

    def tables(self):
        return {
            'ARTRN': list(self.artrn), 'APTRN': list(self.aptrn),
            'STCRD': list(self.stcrd),
            'ARMAS': [{'CUSCOD': 'C001', 'CUSNAM': 'ลูกค้าทดสอบ'}],
            'APMAS': [{'SUPCOD': 'S001', 'SUPNAM': 'ผู้ขายทดสอบ'}],
            'ARTRNRM': [], 'ARRCPIT': [], 'APRCPIT': [],
        }


def run_zip(monkeypatch, book, **kwargs):
    """The daily zip's own entry point, fed `book` instead of DBF files."""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)
    import express_dbf_source as eds
    import import_router
    tables = book.tables()
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: tables[name])
    kwargs.setdefault('since_days', None)
    return import_router.commit_express_dbf('/fake/dataset', **kwargs)


def standard_book(code=CODE):
    """Purchase 100 @10, the sale of 2 โหล @49 (the line under test), then a
    purchase of 100 @20 between the sale and today."""
    return (Book()
            .purchase('RR6900001', [(1, code, 100.0, 'หลอด', 10.0)],
                      datetime.date(2026, 3, 10))
            .sale('IV6900001', [(1, code, 2.0, 'โหล', 49.0)])
            .purchase('RR6900002', [(1, code, 100.0, 'หลอด', 20.0)],
                      datetime.date(2026, 5, 1)))


# ── reading state ────────────────────────────────────────────────────────────

def stock(path, pid):
    c = raw(path)
    try:
        row = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                        (pid,)).fetchone()
        return round(row[0], 4) if row else None
    finally:
        c.close()


def sales_row(path, doc_no, code=CODE):
    c = raw(path)
    try:
        return c.execute("SELECT * FROM sales_transactions WHERE doc_no=? AND bsn_code=?",
                         (doc_no, code)).fetchone()
    finally:
        c.close()


def ledger_rows(path, pid):
    c = raw(path)
    try:
        return c.execute("SELECT * FROM transactions WHERE product_id=? ORDER BY id",
                         (pid,)).fetchall()
    finally:
        c.close()


def sale_ledger(path, doc_no, pid):
    """The line's own ledger rows, as (id, quantity_change)."""
    return [(r['id'], r['quantity_change']) for r in ledger_rows(path, pid)
            if r['reference_no'] == doc_no and r['note'] in ('BSN ขาย', 'BSN ขาย-คืน')]


def offsets(path, pid):
    return [r for r in ledger_rows(path, pid)
            if r['txn_type'] == 'ADJUST' and (r['note'] or '').startswith('แก้หน่วยบรรทัด')]


def corrections(path):
    c = raw(path)
    try:
        return c.execute("SELECT * FROM sales_line_unit_corrections ORDER BY id").fetchall()
    finally:
        c.close()


def cost_ledger(path, pid):
    c = raw(path)
    try:
        return [tuple(r) for r in c.execute(
            "SELECT event_type, event_date, qty_change, unit_cost, stock_after,"
            " wacc_after, reference_no, note FROM product_cost_ledger"
            " WHERE product_id=? ORDER BY id", (pid,))]
    finally:
        c.close()


def alerts(path, kind):
    c = raw(path)
    try:
        return c.execute("SELECT * FROM system_alerts WHERE kind=? ORDER BY id",
                         (kind,)).fetchall()
    finally:
        c.close()


_CHANGE_COLS = ('change_source', 'change_actor', 'change_reason', 'change_token')


def semantic_state(path, pid):
    """The "pre-correction state" of ADR 0021: what cancel must restore.
    Ledger rows as a multiset without ids, the sales rows without their four
    change_* columns, stock at 4 dp, the cost ledger without ids."""
    c = raw(path)
    try:
        sales = sorted(
            tuple((k, r[k]) for k in r.keys() if k not in _CHANGE_COLS)
            for r in c.execute("SELECT * FROM sales_transactions WHERE product_id=?",
                               (pid,)))
        ledger = sorted(tuple(r) for r in c.execute(
            "SELECT product_id, txn_type, quantity_change, reference_no, note,"
            " created_at FROM transactions WHERE product_id=?", (pid,)))
    finally:
        c.close()
    return {'sales': sales, 'ledger': ledger, 'stock': stock(path, pid),
            'cost_ledger': cost_ledger(path, pid)}


def audit_count(path):
    c = raw(path)
    try:
        return c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        c.close()


def written_state(path):
    """Everything a refused correction must leave alone."""
    c = raw(path)
    try:
        return {
            'sales': [tuple(r) for r in c.execute(
                "SELECT * FROM sales_transactions ORDER BY id")],
            'ledger': [tuple(r) for r in c.execute(
                "SELECT * FROM transactions ORDER BY id")],
            'stock': [tuple(r) for r in c.execute(
                "SELECT product_id, quantity FROM stock_levels ORDER BY product_id")],
            'corrections': [tuple(r) for r in c.execute(
                "SELECT * FROM sales_line_unit_corrections ORDER BY id")],
            'cost_ledger': [tuple(r) for r in c.execute(
                "SELECT product_id, event_type, qty_change, stock_after, wacc_after"
                " FROM product_cost_ledger ORDER BY id")],
        }
    finally:
        c.close()
