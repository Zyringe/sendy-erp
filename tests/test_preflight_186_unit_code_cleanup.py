"""scripts/preflight_186_unit_code_cleanup.py: the lever run on a fresh prod
snapshot minutes before 186 is merged. It must report what would block the
migration or move stock, and must never write to the database it inspects."""
import hashlib
import importlib.util
import os
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_spec = importlib.util.spec_from_file_location(
    'preflight_186', os.path.join(REPO, 'scripts', 'preflight_186_unit_code_cleanup.py'))
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _product(conn, name):
    return conn.execute("INSERT INTO products (product_name, unit_type, cost_price) "
                        "VALUES (?, 'ตัว', 1)", (name,)).lastrowid


def test_reports_a_precondition_violator_and_leaves_the_source_alone(tmp_db):
    conn = sqlite3.connect(tmp_db)
    pid = _product(conn, 'preflight186 conflict')
    bad = conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                       "VALUES (?, 'หล', 12)", (pid,)).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 10)",
                 (pid,))
    conn.commit()
    conn.close()
    before = _sha(tmp_db)

    code, report = preflight.run(tmp_db)

    assert code == 1
    assert [bad, pid, 'หล', 12.0, 'โหล', 10.0] in report['violators']['_mig186_pre_twin_ratio']
    assert 'twin_ratio' in report['migration_error']
    assert _sha(tmp_db) == before


def test_reports_a_line_the_migration_would_make_syncable(tmp_db):
    """A raw 'หล' line with only a 'โหล' conversion cannot sync today; after 186
    it can, and the next save on /unit-conversions would post it. The
    migration itself allows this, so only the preflight can see it."""
    conn = sqlite3.connect(tmp_db)
    pid = _product(conn, 'preflight186 newly syncable')
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)",
                 (pid,))
    sid = conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01','IVPF-1','IVPF',?,'PF186','x','c','C1',2,'หล',10,0,'',20,20,0)",
        (pid,)).lastrowid
    conn.commit()
    conn.close()

    code, report = preflight.run(tmp_db)

    assert code == 1
    assert ['sales_transactions', sid, None, 24.0] in report['lines_newly_syncable']
    assert report['changed']['sales_transactions.unit'] >= 1
