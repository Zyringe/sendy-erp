"""TDD for #497 milestone 2 — call_card.get_card switched to the shared
winback.compute_winback, scoped by customer_code (never by bill name).

Prior bug (the issue's own BUG 2 case): `_compute_winback` queried
`sales_transactions.customer IN (names)`, so two customer CODES sharing one
bill name (e.g. ทรัพย์ทวี) had their win-back merged into one list. This
pins the fix at the call_card.get_card level, independent of the customer
page (see test_497_customer_page_winback_notes.py for that surface).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

SENDAI_BRAND_ID = 3

SHARED_NAME = 'ร้านทดสอบ 497 ชื่อซ้ำ'
CODE_A = 'TEST497A'
CODE_B = 'TEST497B'

_pid_counter = [497100]


def _mk_product(conn, name='สินค้าทดสอบ 497 call-card'):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", 'ตัว', 100.0, 60.0, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code, name):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()


def _clear(conn):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code IN (?, ?)",
                 (CODE_A, CODE_B))
    conn.commit()


def _line(conn, *, doc_base, pid, date_iso, code):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,1,'ตัว',100,0,100,100)",
        (date_iso, f'{doc_base}-1', doc_base, pid, SHARED_NAME, code),
    )
    conn.commit()


@pytest.fixture
def scenario(tmp_db_conn):
    conn = tmp_db_conn
    _mk_customer(conn, CODE_A, SHARED_NAME)
    _mk_customer(conn, CODE_B, SHARED_NAME)
    _clear(conn)
    pid_a = _mk_product(conn, name='สินค้าเฉพาะรหัสเอ')
    pid_b = _mk_product(conn, name='สินค้าเฉพาะรหัสบี')
    # CODE_A: winback-eligible history for pid_a only.
    # Eligibility is >=3 DISTINCT INVOICES (doc_base), so each line needs its
    # own doc_base — `d[-2:]` used to collide on '01' for every date here
    # (all three dates end in day "01") before this was caught.
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IVA{i}', pid=pid_a, date_iso=d, code=CODE_A)
    # CODE_B: winback-eligible history for pid_b only.
    for i, d in enumerate(['2020-01-01', '2020-02-01', '2020-03-01']):
        _line(conn, doc_base=f'IVB{i}', pid=pid_b, date_iso=d, code=CODE_B)
    yield conn, pid_a, pid_b
    _clear(conn)


def test_shared_bill_name_does_not_merge_winback_across_codes(scenario):
    conn, pid_a, pid_b = scenario
    import call_card as cc

    card_a = cc.get_card(conn, CODE_A)
    card_b = cc.get_card(conn, CODE_B)

    keys_a = {(w['product_id'], w['unit']) for w in card_a['winback']}
    keys_b = {(w['product_id'], w['unit']) for w in card_b['winback']}

    # Control: each code's own product IS flagged under its own code.
    assert (pid_a, 'ตัว') in keys_a
    assert (pid_b, 'ตัว') in keys_b
    # The actual assertion: CODE_B's product must never leak into CODE_A's
    # win-back (and vice versa) just because they share a bill name.
    assert (pid_b, 'ตัว') not in keys_a
    assert (pid_a, 'ตัว') not in keys_b
