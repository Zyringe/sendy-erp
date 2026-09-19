"""#589 — mig 189 moves ทวีเกียรติ (salesperson 03) from Tier C (0%) to Tier A.

Put's ruling 2026-09-19: 03 is paid through /commission from now on, at Tier A
(10% own / 5% third party). The live DB a test clones may already carry 189
(anything that imports `app` runs init_db), so every test first runs the
rollback to put the clone back in the pre-189 state, then applies the file.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MIG = os.path.join(REPO, 'data', 'migrations', '189_commission_03_tier_a.sql')
ROLLBACK = os.path.join(REPO, 'data', 'migrations', '189_commission_03_tier_a.rollback.sql')
NOTE_AFTER = 'ท /03 — ทวีเกียรติ (Tier A, Put 2026-09-19, #589)'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


@pytest.fixture
def pre189(tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_read(ROLLBACK))
    yield conn
    conn.close()


def _tier_of(conn, sp):
    row = conn.execute(
        "SELECT t.code, a.note, a.effective_from, a.updated_at FROM commission_assignments a "
        "JOIN commission_tiers t ON t.id = a.tier_id WHERE a.salesperson_code = ?", (sp,)
    ).fetchone()
    return dict(row) if row else None


def _others(conn):
    return conn.execute(
        "SELECT salesperson_code, tier_id, effective_from, note, updated_at "
        "FROM commission_assignments WHERE salesperson_code <> '03' ORDER BY 1"
    ).fetchall()


def _assignment_update_audits(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name = 'commission_assignments' "
        "AND action = 'UPDATE'"
    ).fetchone()[0]


def test_mig189_moves_03_to_tier_a_and_nothing_else(pre189):
    conn = pre189
    before = _tier_of(conn, '03')
    assert before['code'] == 'C', 'rollback must leave the clone on the pre-189 tier'
    others_before = [tuple(r) for r in _others(conn)]
    assert len(others_before) >= 11, 'control: the other reps are present to compare'

    conn.executescript(_read(MIG))

    after = _tier_of(conn, '03')
    assert after['code'] == 'A'
    assert after['note'] == NOTE_AFTER
    assert after['effective_from'] == before['effective_from'], \
        'effective_from is left alone: _load_tiers ignores it'
    assert [tuple(r) for r in _others(conn)] == others_before


def test_mig189_is_rerunnable_and_audits_the_change_once(pre189):
    conn = pre189
    n0 = _assignment_update_audits(conn)
    conn.executescript(_read(MIG))
    first = _tier_of(conn, '03')
    n1 = _assignment_update_audits(conn)
    conn.executescript(_read(MIG))
    second = _tier_of(conn, '03')
    n2 = _assignment_update_audits(conn)

    assert first['code'] == second['code'] == 'A'
    assert n1 - n0 == 1, 'the real change is audited'
    assert n2 == n1, 'a re-run changes nothing, so it logs nothing'
    assert second['updated_at'] == first['updated_at'], 'a re-run does not bump updated_at'


def test_mig189_precondition_aborts_when_03_row_is_missing(pre189):
    conn = pre189
    conn.execute("DELETE FROM commission_assignments WHERE salesperson_code = '03'")
    conn.commit()
    others_before = [tuple(r) for r in _others(conn)]

    with pytest.raises(sqlite3.DatabaseError, match='mig 189 precondition FAILED'):
        conn.executescript(_read(MIG))
    conn.rollback()

    assert _tier_of(conn, '03') is None, 'nothing was inserted in its place'
    assert [tuple(r) for r in _others(conn)] == others_before
    assert conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE filename = '189_commission_03_tier_a.sql'"
    ).fetchone()[0] == 0


def test_rollback_restores_tier_c_and_unstamps(pre189):
    conn = pre189
    conn.executescript(_read(MIG))
    conn.execute(
        "INSERT OR IGNORE INTO applied_migrations (filename, applied_by, sha256) "
        "VALUES ('189_commission_03_tier_a.sql', 'test', 'x')")
    conn.commit()

    conn.executescript(_read(ROLLBACK))

    row = _tier_of(conn, '03')
    assert row['code'] == 'C' and row['note'] == 'ท /03 — TBD'
    assert conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE filename = '189_commission_03_tier_a.sql'"
    ).fetchone()[0] == 0


# ── The engine reads the new tier ────────────────────────────────────────────

def _product_without_override(conn, own):
    row = conn.execute("""
        SELECT p.id, p.product_name FROM products p JOIN brands b ON b.id = p.brand_id
         WHERE b.is_own_brand = ?
           AND p.id NOT IN (SELECT product_id FROM commission_overrides WHERE product_id IS NOT NULL)
           AND p.brand_id NOT IN (SELECT brand_id FROM commission_overrides WHERE brand_id IS NOT NULL)
         ORDER BY p.id LIMIT 1""", (1 if own else 0,)).fetchone()
    if row is None:
        pytest.skip('no product without a commission override for this brand kind')
    return row


def _seed_invoice(conn, sp, doc, receipt_date, lines):
    cur = conn.execute(
        "INSERT INTO received_payments (re_no, date_iso, customer, salesperson, cancelled, total) "
        "VALUES (?, ?, 'ลูกค้าทดสอบ 589', ?, 0, ?)",
        ('RE' + doc, receipt_date, sp, sum(n for _, n in lines)))
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?, ?, 'IV', ?)",
        (cur.lastrowid, doc, sum(n for _, n in lines)))
    for i, (product, net) in enumerate(lines, 1):
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, bsn_code, "
            "product_name_raw, customer, customer_code, qty, unit, unit_price, total, net) "
            "VALUES (?, ?, ?, ?, 'T589', ?, 'ลูกค้าทดสอบ 589', 'T589', 1, 'ตัว', ?, ?, ?)",
            (receipt_date, f'{doc}-{i}', doc, product['id'], product['product_name'], net, net, net))
    conn.commit()


def test_engine_owes_03_tier_a_rates_after_mig189(pre189, tmp_db):
    import commission
    conn = pre189
    own = _product_without_override(conn, own=True)
    third = _product_without_override(conn, own=False)
    _seed_invoice(conn, '03', 'IV9589001', '2031-01-15', [(own, 1000.0), (third, 400.0)])
    _seed_invoice(conn, '06', 'IV9589002', '2031-01-16', [(own, 1000.0)])

    def due(sp, doc):
        rows = [r for r in commission.get_invoice_commission_for_sp('2031-01', sp, db_path=tmp_db)
                if r['invoice_no'] == doc]
        assert len(rows) == 1
        return rows[0]

    before = due('03', 'IV9589001')
    assert before['commission_due'] == 0 and before['paid_status'] == 'no_rate'
    control_before = due('06', 'IV9589002')['commission_due']

    conn.executescript(_read(MIG))

    after = due('03', 'IV9589001')
    assert after['commission_due'] == 120.0, '10% of 1,000 own + 5% of 400 third'
    assert after['paid_status'] == 'pending'
    assert due('06', 'IV9589002')['commission_due'] == control_before == 100.0
