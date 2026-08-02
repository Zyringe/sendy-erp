"""`/customers` search must not change its meaning when a customer's first
bill lands.

`get_customers` is a UNION of two halves and, before this change, each half
matched a DIFFERENT name column:

    billing half   →  s.customer      (the name typed on the bill)
    bill-less half →  c.name          (the name on the customers master)

Those two strings disagree for 198 of the 276 billing customers in the live
snapshot, essentially always because the master carries a legal prefix the
bill drops: master `หจก. ไทยทวีกิจ` vs bill `ไทยทวีกิจ`. Measured on the
same snapshot the `tmp_db` fixture copies:

    q = 'หจก. ไทยทวีกิจ'  →  0 rows   (billing half, before)
                          →  1 row    (billing half, with c.name)

So a customer the user found yesterday by its registered name — while it was
still bill-less — silently stops matching that search the moment it bills.
The fix adds `c.name` to the billing half, making the master name a valid
search key on BOTH sides.

`c.code` is deliberately NOT added alongside it: the billing half already
joins `c.code = s.customer_code` and searches `s.customer_code`, so a second
predicate on `c.code` could only match rows the first one already matched.

Every figure below is derived from the tmp_db snapshot at run time, never
hardcoded, so this file cannot rot as the data moves.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3


def _raw_conn(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    return conn


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _customer_billing_under_a_different_name(conn):
    """A customer that HAS bills and whose master name appears on NONE of
    them — exactly the shape the billing-half search used to miss.

    Two things this picker must get right or it stops testing the bug:

      * `NOT EXISTS (… instr(s.customer, c.name) …)` covers EVERY bill row,
        not one representative name. A customer whose bills carry several
        spellings could have one that DOES contain the master name, which
        would make it reachable before the fix and the test vacuous.
      * the bills it counts are filtered by the same `not_a_sale_clause` that
        `get_customers` applies, so this can never hand back a customer the
        function under test deliberately never returns (0 such codes today,
        but a write-off-only customer would turn into a confusing red).

    Returns a row (code, bill_name, master_name) or None.
    """
    import sales_filters
    real_sale = sales_filters.not_a_sale_clause('s')
    return conn.execute(f"""
        SELECT c.code AS code,
               c.name AS master_name,
               (SELECT MAX(s.customer) FROM sales_transactions s
                WHERE s.customer_code = c.code AND {real_sale}) AS bill_name
        FROM customers c
        WHERE c.name IS NOT NULL AND c.name <> ''
          AND EXISTS (SELECT 1 FROM sales_transactions s
                      WHERE s.customer_code = c.code AND {real_sale})
          AND NOT EXISTS (SELECT 1 FROM sales_transactions s
                          WHERE s.customer_code = c.code AND {real_sale}
                            AND instr(s.customer, c.name) > 0)
        ORDER BY c.code
        LIMIT 1
    """).fetchone()


def test_the_snapshot_actually_contains_the_shape_under_test(tmp_db):
    """Guard: if this DB had no divergent-name billing customer, every test
    below would pass by finding nothing to break. Fail loudly instead."""
    conn = _raw_conn(tmp_db)
    sample = _customer_billing_under_a_different_name(conn)
    conn.close()
    assert sample is not None, (
        "no billing customer whose bill name omits its master name — the "
        "asymmetry these tests pin cannot be observed on this snapshot")


def test_billing_customer_is_findable_by_its_registered_name(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    sample = _customer_billing_under_a_different_name(conn)
    conn.close()

    rows, total = models.get_customers(search=sample['master_name'],
                                       per_page=5000)
    codes = {r['customer_code'] for r in rows}
    assert sample['code'] in codes, (
        f"master name {sample['master_name']!r} does not reach "
        f"{sample['code']} (bills as {sample['bill_name']!r})")
    assert total == len(rows), (
        "the total counts a different row set than the page renders")


def test_registered_name_search_survives_the_customers_first_bill(tmp_db):
    """The invariant that matters: a search that finds a customer while it is
    bill-less must still find it once it bills."""
    import models
    conn = _raw_conn(tmp_db)
    billless = conn.execute("""
        SELECT code, name FROM customers c
        WHERE c.name LIKE 'ร้าน %'
          AND NOT EXISTS (SELECT 1 FROM sales_transactions s
                          WHERE s.customer_code = c.code)
        ORDER BY code LIMIT 1
    """).fetchone()
    assert billless is not None, "snapshot has no bill-less 'ร้าน …' customer"

    before, _ = models.get_customers(search=billless['name'],
                                     include_billless=True, per_page=5000)
    assert billless['code'] in {r['customer_code'] for r in before}, (
        "bill-less half already fails to match the master name — the premise "
        "of this test is wrong")

    # Its first bill lands. Express writes the name as it appears on the bill,
    # which drops the 'ร้าน ' prefix the master record carries.
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, customer, customer_code, qty, unit_price, net) "
        "VALUES (?, ?, ?, ?, 1, 100, 100)",
        ('2026-08-03', 'IV-SEARCH-SYM-1',
         billless['name'].replace('ร้าน ', '', 1), billless['code']))
    conn.commit()
    conn.close()

    after, _ = models.get_customers(search=billless['name'],
                                    include_billless=True, per_page=5000)
    assert billless['code'] in {r['customer_code'] for r in after}, (
        f"{billless['code']} was findable by {billless['name']!r} until its "
        "first bill moved it to the billing half, which searches a different "
        "name column")


def test_search_by_bill_name_still_works(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    sample = _customer_billing_under_a_different_name(conn)
    conn.close()

    rows, _ = models.get_customers(search=sample['bill_name'], per_page=5000)
    assert sample['code'] in {r['customer_code'] for r in rows}


def test_search_by_customer_code_still_works(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    sample = _customer_billing_under_a_different_name(conn)
    conn.close()

    rows, total = models.get_customers(search=sample['code'], per_page=5000)
    assert sample['code'] in {r['customer_code'] for r in rows}
    assert total == len(rows)


def test_customers_page_renders_the_row_for_a_registered_name_search(tmp_db):
    """The real HTTP path, not just the model — /customers?q=<master name>."""
    from urllib.parse import quote
    conn = _raw_conn(tmp_db)
    sample = _customer_billing_under_a_different_name(conn)
    conn.close()

    c = _client(tmp_db, role='admin')
    html = c.get('/customers?q=' + quote(sample['master_name'])).data.decode()
    assert f'font-mono">{sample["code"]}</span>' in html
    assert 'ไม่พบลูกค้าที่ตรงกับการค้นหา' not in html
