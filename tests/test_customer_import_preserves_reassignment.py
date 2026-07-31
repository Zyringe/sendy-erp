"""A customer-master import must not undo a commission reassignment.

`import_customers_from_bsn` refreshes `customers.salesperson` from the source
file on BOTH update paths (protected and normal). Express still lists departed
reps as the owner — น้อย /02 is on 168 customers there — so importing would
silently reset every customer moved to the company by migrations 143/144.

That is the same shape as the bug this whole arc started from: an import
overwriting a decision (`received_payments.salesperson` is UPSERT-ed on every
weekly import, which is why the reassignment lives in its own table rather than
being edited into the receipt).

The reassignment is a decision Put made AFTER the import file was produced, so
it wins. The import refreshes name/address/contact as normal, then the
reassignment is re-applied on top.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


def _seed(conn):
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('02','น้อย'),('00','บริษัท')")
    conn.execute(
        "INSERT INTO customers (code, name, salesperson, zone, customer_type, credit_days) "
        "VALUES ('C-MOVED','ลูกค้าที่ย้ายแล้ว','00','ขน','01',30)")
    conn.execute(
        "INSERT INTO commission_customer_reassign "
        "(customer_code, to_salesperson, effective_from) VALUES ('C-MOVED','00','2026-02-01')")
    conn.commit()


def _bsn_row(code, salesperson):
    return {'code': code, 'name': 'ลูกค้าที่ย้ายแล้ว', 'salesperson': salesperson,
            'zone': 'ขน', 'customer_type': '01', 'credit_days': 30,
            'tax_id': '', 'contact': '', 'address': '', 'phone': '', 'fax': ''}


def _owner(conn, code='C-MOVED'):
    return conn.execute("SELECT salesperson FROM customers WHERE code=?", (code,)).fetchone()[0]


def test_import_does_not_revert_a_reassigned_customer(empty_db, empty_db_conn):
    """Express still says น้อย owns this customer. The reassignment must win."""
    import models

    _seed(empty_db_conn)
    assert _owner(empty_db_conn) == '00'

    models.import_customers_from_bsn([_bsn_row('C-MOVED', '02')])

    assert _owner(empty_db_conn) == '00', (
        "import reset the customer to น้อย /02 — every reassignment would be "
        "undone by the next customer-master import")


def test_import_still_updates_customers_without_a_rule(empty_db, empty_db_conn):
    """Guard against over-correcting: ordinary customers must still follow the
    source file, or the import stops doing its job."""
    import models

    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO customers (code, name, salesperson, zone, customer_type, credit_days) "
        "VALUES ('C-PLAIN','ลูกค้าปกติ','02','ขน','01',30)")
    empty_db_conn.execute("INSERT INTO salespersons (code, name) VALUES ('06','ต๋อ')")
    empty_db_conn.commit()

    models.import_customers_from_bsn([_bsn_row('C-PLAIN', '06')])

    assert _owner(empty_db_conn, 'C-PLAIN') == '06', (
        "a customer with no reassignment rule must follow the source file")


def test_inactive_rule_does_not_protect(empty_db, empty_db_conn):
    """A switched-off rule is not a decision in force."""
    import models

    _seed(empty_db_conn)
    empty_db_conn.execute("UPDATE commission_customer_reassign SET is_active=0")
    empty_db_conn.commit()

    models.import_customers_from_bsn([_bsn_row('C-MOVED', '02')])

    assert _owner(empty_db_conn) == '02', (
        "an inactive rule must not keep protecting the customer")


def test_import_does_not_churn_the_audit_trail(empty_db, empty_db_conn):
    """The rule must be applied BEFORE the write, not corrected afterwards.

    Writing the file's value and fixing it after lands the right data but fires
    `audit_customers_update` twice, recording a change that never happened. The
    first cut of this guard did exactly that: 936 phantom rows (468 out, 468
    back) inside a 2,718-row import — 34% noise in the trail people rely on to
    answer "who changed this customer".
    """
    import models

    _seed(empty_db_conn)
    empty_db_conn.execute("DELETE FROM audit_log")
    empty_db_conn.commit()

    # Express still says น้อย owns it; nothing about the customer actually changes.
    models.import_customers_from_bsn([_bsn_row('C-MOVED', '02')])

    rows = empty_db_conn.execute(
        "SELECT changed_fields FROM audit_log WHERE table_name='customers'").fetchall()
    touching_sp = [r[0] for r in rows if 'salesperson' in (r[0] or '')]
    assert not touching_sp, (
        f"import recorded a salesperson change that never happened: {touching_sp}")
    assert _owner(empty_db_conn) == '00'
