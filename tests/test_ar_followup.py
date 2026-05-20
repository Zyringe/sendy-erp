"""TDD tests for inventory_app/ar_followup.py — AR follow-up workspace logic.

Synthetic data only (empty_db_conn schema clone). Pattern mirrors
test_cashflow.py and test_payments_alloc.py.

Logic under test
────────────────
customer_ranking(conn):
  - Aggregates payments_alloc.invoice_settlement by customer.
  - Returns rows with outstanding > 0 sorted by outstanding DESC.
  - Each row carries: customer, customer_code, invoice_count, outstanding,
    oldest_age_days, age_buckets {0-30,31-60,61-90,90+}, last_log (date/result).

log_outreach(conn, ...):
  - Inserts into ar_followup_log; rejects bad channel/result enums.

get_customer_followups(conn, customer):
  - Returns chronological list (log_date DESC) for one customer.

get_customer_ar_detail(conn, customer):
  - Returns list of outstanding invoices for one customer with age.
"""
import pytest
from datetime import date, timedelta

import ar_followup as arf


# ── synthetic data helpers ──────────────────────────────────────────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-{line}", doc_base, customer, customer_code,
         1, 'ตัว', net, vat_type, net, net),
    )


def _ins_receipt(conn, re_no, customer, date_iso, cancelled=0, total=None):
    cur = conn.execute(
        """INSERT INTO received_payments
           (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?,?,?,?,?,?)""",
        (re_no, date_iso, customer, 'S1', cancelled, total),
    )
    return cur.lastrowid


def _ins_paid(conn, re_id, iv_no, amount):
    conn.execute(
        "INSERT INTO paid_invoices (re_id, iv_no, amount) VALUES (?,?,?)",
        (re_id, iv_no, amount),
    )


# ── customer_ranking ────────────────────────────────────────────────────────

def test_ranking_sorts_by_outstanding_desc(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A',  'CA', today, 1000)
    _ins_sale(c, 'IV02', 'B',  'CB', today, 5000)
    _ins_sale(c, 'IV03', 'C',  'CC', today, 3000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    customers = [r['customer'] for r in rows]
    assert customers == ['B', 'C', 'A']
    assert rows[0]['outstanding'] == pytest.approx(5000)


def test_ranking_excludes_fully_paid(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000)
    _ins_sale(c, 'IV02', 'B', 'CB', today, 2000)
    rid = _ins_receipt(c, 'RE01', 'A', today, total=1000)
    _ins_paid(c, rid, 'IV01', 1000)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert [r['customer'] for r in rows] == ['B']


def test_ranking_buckets_by_age(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    _ins_sale(c, 'IV01', 'A', 'CA', (today - timedelta(days=10)).isoformat(), 100)   # 0-30
    _ins_sale(c, 'IV02', 'A', 'CA', (today - timedelta(days=45)).isoformat(), 200)   # 31-60
    _ins_sale(c, 'IV03', 'A', 'CA', (today - timedelta(days=75)).isoformat(), 300)   # 61-90
    _ins_sale(c, 'IV04', 'A', 'CA', (today - timedelta(days=180)).isoformat(), 400)  # 90+
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    r = rows[0]
    assert r['outstanding'] == pytest.approx(1000)
    assert r['invoice_count'] == 4
    assert r['oldest_age_days'] == 180
    assert r['age_buckets']['0-30'] == pytest.approx(100)
    assert r['age_buckets']['31-60'] == pytest.approx(200)
    assert r['age_buckets']['61-90'] == pytest.approx(300)
    assert r['age_buckets']['90+'] == pytest.approx(400)


def test_ranking_vat_type_2_adds_vat(empty_db_conn):
    """vat_type=2 (แยก VAT) — billed must be net*1.07 per codebase idiom."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000, vat_type=2)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert rows[0]['outstanding'] == pytest.approx(1070.00, abs=0.01)


# ── log_outreach ────────────────────────────────────────────────────────────

def test_log_outreach_inserts_row(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    log_id = arf.log_outreach(
        conn=c, customer='A', customer_code='CA', log_date=today,
        channel='phone', contact_person='คุณสมศักดิ์', result='promised',
        promised_amount=5000.00, promised_date=today,
        next_action_date=(date.today() + timedelta(days=7)).isoformat(),
        notes='นัดจ่าย', created_by='admin',
    )
    c.commit()
    row = c.execute(
        "SELECT * FROM ar_followup_log WHERE id=?", (log_id,)
    ).fetchone()
    assert row['customer'] == 'A'
    assert row['channel'] == 'phone'
    assert row['result'] == 'promised'
    assert row['promised_amount'] == pytest.approx(5000)


def test_log_outreach_rejects_bad_channel(empty_db_conn):
    import sqlite3 as _sq
    c = empty_db_conn
    with pytest.raises(_sq.IntegrityError):
        arf.log_outreach(
            conn=c, customer='A', customer_code='CA',
            log_date=date.today().isoformat(),
            channel='telegram', result='promised', created_by='admin',
        )


def test_log_outreach_rejects_bad_result(empty_db_conn):
    import sqlite3 as _sq
    c = empty_db_conn
    with pytest.raises(_sq.IntegrityError):
        arf.log_outreach(
            conn=c, customer='A', customer_code='CA',
            log_date=date.today().isoformat(),
            channel='phone', result='maybe', created_by='admin',
        )


# ── get_customer_followups ──────────────────────────────────────────────────

def test_get_followups_returns_newest_first(empty_db_conn):
    c = empty_db_conn
    old = (date.today() - timedelta(days=10)).isoformat()
    new = date.today().isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=old,
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=new,
                     channel='line', result='promised', created_by='admin')
    c.commit()

    rows = arf.get_customer_followups(conn=c, customer='A')
    assert len(rows) == 2
    assert rows[0]['log_date'] == new
    assert rows[0]['channel'] == 'line'


def test_get_followups_isolates_per_customer(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=today,
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='B', customer_code='CB', log_date=today,
                     channel='line', result='promised', created_by='admin')
    c.commit()

    assert len(arf.get_customer_followups(conn=c, customer='A')) == 1
    assert len(arf.get_customer_followups(conn=c, customer='B')) == 1


# ── get_customer_ar_detail ──────────────────────────────────────────────────

def test_get_customer_ar_detail_lists_outstanding(empty_db_conn):
    c = empty_db_conn
    today = date.today()
    _ins_sale(c, 'IV01', 'A', 'CA', (today - timedelta(days=5)).isoformat(), 1000)
    _ins_sale(c, 'IV02', 'A', 'CA', (today - timedelta(days=200)).isoformat(), 2000)
    _ins_sale(c, 'IV03', 'B', 'CB', today.isoformat(), 500)
    c.commit()

    rows = arf.get_customer_ar_detail(conn=c, customer='A')
    docs = sorted(r['doc_base'] for r in rows)
    assert docs == ['IV01', 'IV02']
    # age should be present
    iv02 = next(r for r in rows if r['doc_base'] == 'IV02')
    assert iv02['age_days'] == 200


# ── ranking joins last_log ──────────────────────────────────────────────────

def test_ranking_includes_last_log(empty_db_conn):
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'A', 'CA', today, 1000)
    arf.log_outreach(conn=c, customer='A', customer_code='CA', log_date=today,
                     channel='phone', result='promised',
                     next_action_date=today, created_by='admin')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert rows[0]['last_log_date'] == today
    assert rows[0]['last_log_result'] == 'promised'


# ── customer-identity (customer_code as stable key) ─────────────────────────
# Codex adversarial-review finding (HIGH): aggregating by customer NAME splits
# one debtor across name spellings and orphans follow-up history. Key by
# customer_code where present.

def test_ranking_aggregates_by_customer_code_when_names_differ(empty_db_conn):
    """Same customer_code, two name spellings (trailing space) → ONE row."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['outstanding'] == pytest.approx(3000)
    assert rows[0]['customer_code'] == 'CA'
    assert rows[0]['invoice_count'] == 2


def test_ranking_falls_back_to_name_when_no_code(empty_db_conn):
    """No customer_code (walk-in) → group by name."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'หน้าร้าน',   None, today, 100, line=1)
    _ins_sale(c, 'IV02', 'หน้าร้าน',   None, today, 200, line=1)
    _ins_sale(c, 'IV03', 'หน้าร้าน 2', None, today, 50,  line=1)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 2
    by_name = {r['customer']: r['outstanding'] for r in rows}
    assert by_name['หน้าร้าน']   == pytest.approx(300)
    assert by_name['หน้าร้าน 2'] == pytest.approx(50)


def test_ranking_canonical_name_from_most_recent_invoice(empty_db_conn):
    """When same code has multiple name spellings, ranking row shows the
    name from the most-recent invoice (deterministic display)."""
    c = empty_db_conn
    today = date.today()
    old   = (today - timedelta(days=60)).isoformat()
    newer = (today - timedelta(days=5)).isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A (เก่า)',  'CA', old,   1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A (ใหม่)', 'CA', newer, 500,  line=1)
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['customer'] == 'ลูกค้า A (ใหม่)'


def test_detail_finds_all_name_variants_for_code(empty_db_conn):
    """Detail lookup with EITHER name spelling returns BOTH invoices."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    c.commit()

    rows_a = arf.get_customer_ar_detail(customer='ลูกค้า A',  conn=c)
    rows_b = arf.get_customer_ar_detail(customer='ลูกค้า A ', conn=c)
    assert sorted(r['doc_base'] for r in rows_a) == ['IV01', 'IV02']
    assert sorted(r['doc_base'] for r in rows_b) == ['IV01', 'IV02']


def test_followups_finds_all_name_variants_for_code(empty_db_conn):
    """Followup history spans all name spellings sharing customer_code."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    arf.log_outreach(conn=c, customer='ลูกค้า A',  customer_code='CA',
                     log_date=today, channel='phone',
                     result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='ลูกค้า A ', customer_code='CA',
                     log_date=today, channel='line',
                     result='promised',  created_by='admin')
    c.commit()

    rows = arf.get_customer_followups(customer='ลูกค้า A', conn=c)
    assert len(rows) == 2


def test_ranking_last_log_aggregates_by_group(empty_db_conn):
    """Ranking's last_log uses the newest log across all name variants of
    the same customer_code (not whatever spelling happens to be alphabetized first)."""
    c = empty_db_conn
    today = date.today()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today.isoformat(), 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today.isoformat(), 2000, line=1)
    arf.log_outreach(conn=c, customer='ลูกค้า A',  customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='ลูกค้า A ', customer_code='CA',
                     log_date=today.isoformat(),
                     channel='line',  result='promised',  created_by='admin')
    c.commit()

    rows = arf.customer_ranking(conn=c)
    assert len(rows) == 1
    assert rows[0]['last_log_date']   == today.isoformat()
    assert rows[0]['last_log_result'] == 'promised'


# ── overdue (supersession + terminal-state awareness) ───────────────────────
# Codex adversarial-review finding (MEDIUM): list_overdue_followups blindly
# returned every row whose next_action_date had passed. Only the LATEST log
# per customer-group counts, and terminal results (paid_full / closed) close
# the loop.

def test_overdue_excludes_superseded_by_later_log(empty_db_conn):
    """A later log rescheduling to future supersedes the older overdue row."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer',
                     next_action_date=(today + timedelta(days=7)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_excludes_terminal_paid_full(empty_db_conn):
    """Latest log = paid_full → debt closed, never overdue."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=10)).isoformat(),
                     channel='phone', result='paid_full',
                     next_action_date=(today - timedelta(days=5)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_excludes_terminal_closed(empty_db_conn):
    """Latest log = closed → account closed, never overdue."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=10)).isoformat(),
                     channel='phone', result='closed',
                     next_action_date=(today - timedelta(days=5)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_includes_only_unresolved_past_due(empty_db_conn):
    """Mix: only customer A (unresolved past-due) should appear; B (paid_full)
    and C (future next-action) excluded."""
    c = empty_db_conn
    today  = date.today()
    past   = (today - timedelta(days=5)).isoformat()
    future = (today + timedelta(days=5)).isoformat()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=past, channel='phone', result='promised',
                     next_action_date=past,   created_by='admin')
    arf.log_outreach(conn=c, customer='B', customer_code='CB',
                     log_date=past, channel='phone', result='paid_full',
                     next_action_date=past,   created_by='admin')
    arf.log_outreach(conn=c, customer='C', customer_code='CC',
                     log_date=past, channel='phone', result='promised',
                     next_action_date=future, created_by='admin')
    c.commit()

    overdue = arf.list_overdue_followups(conn=c, as_of=today.isoformat())
    assert [r['customer'] for r in overdue] == ['A']


# ── overdue: fall-back to prior log when latest has NULL next_action_date ───
# Scrutinize finding 1 (major): a latest log without a planned follow-up date
# would silently swallow customers whose prior log left a past-due obligation.
# The intent ("which customers need attention?") demands that the prior plan
# stays visible until something terminal (paid_full / closed) lands.

def test_overdue_falls_back_to_prior_when_latest_has_null(empty_db_conn):
    """Latest log = no_answer with no follow-up date → keep showing the
    older past-due obligation; customer is NOT resolved."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=5)).isoformat(),
                     channel='phone', result='no_answer',
                     next_action_date=None, created_by='admin')
    c.commit()

    overdue = arf.list_overdue_followups(conn=c, as_of=today.isoformat())
    customers = [r['customer'] for r in overdue]
    assert customers == ['A'], (
        "Customer A has unresolved past-due plan from prior log; "
        "latest log set no new follow-up date — must not vanish."
    )
    # And the row carries the prior plan's next_action_date, not None.
    assert overdue[0]['next_action_date'] == (today - timedelta(days=10)).isoformat()


def test_overdue_excludes_when_latest_terminal_even_with_prior_action_date(empty_db_conn):
    """Latest log = paid_full (NULL next_action) overrides a prior past-due
    plan; customer is closed, must not appear."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=2)).isoformat(),
                     channel='visit', result='paid_full',
                     next_action_date=None, created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


def test_overdue_uses_latest_action_date_when_present(empty_db_conn):
    """When the latest log has its own non-NULL next_action_date in the
    future, the prior past-due plan is superseded (the staff explicitly
    rescheduled). Customer NOT overdue."""
    c = empty_db_conn
    today = date.today()
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=20)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today - timedelta(days=10)).isoformat(),
                     created_by='admin')
    arf.log_outreach(conn=c, customer='A', customer_code='CA',
                     log_date=(today - timedelta(days=2)).isoformat(),
                     channel='phone', result='promised',
                     next_action_date=(today + timedelta(days=7)).isoformat(),
                     created_by='admin')
    c.commit()

    assert arf.list_overdue_followups(conn=c, as_of=today.isoformat()) == []


# ── _resolve_target: route key can be either customer_code OR name ──────────
# Scrutinize finding 2 (major) + 3 (nit): URL keys must be stable across
# typo-fixes upstream. customer_code is the right primary key when present.

def test_resolve_target_accepts_customer_code(empty_db_conn):
    """A bare customer_code string resolves to the full name-set of that code."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    c.commit()

    code, names = arf._resolve_target(c, 'CA')
    assert code == 'CA'
    assert set(names) == {'ลูกค้า A', 'ลูกค้า A '}


def test_resolve_target_falls_back_to_name(empty_db_conn):
    """Old name-based bookmark / walk-in still resolves correctly."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'หน้าร้าน', None, today, 100, line=1)
    c.commit()

    code, names = arf._resolve_target(c, 'หน้าร้าน')
    assert code is None
    assert names == ['หน้าร้าน']


def test_resolve_target_code_lookup_finds_all_invoices(empty_db_conn):
    """Detail lookup via customer_code returns BOTH variant invoices."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    c.commit()

    # Detail lookup uses _resolve_target under the hood now.
    rows = arf.get_customer_ar_detail(customer='CA', conn=c)
    assert sorted(r['doc_base'] for r in rows) == ['IV01', 'IV02']


def test_resolve_target_code_lookup_finds_all_followups(empty_db_conn):
    """Followup lookup via customer_code returns BOTH variant logs."""
    c = empty_db_conn
    today = date.today().isoformat()
    _ins_sale(c, 'IV01', 'ลูกค้า A',  'CA', today, 1000, line=1)
    _ins_sale(c, 'IV02', 'ลูกค้า A ', 'CA', today, 2000, line=1)
    arf.log_outreach(conn=c, customer='ลูกค้า A',  customer_code='CA',
                     log_date=today, channel='phone',
                     result='no_answer', created_by='admin')
    arf.log_outreach(conn=c, customer='ลูกค้า A ', customer_code='CA',
                     log_date=today, channel='line',
                     result='promised', created_by='admin')
    c.commit()

    rows = arf.get_customer_followups(customer='CA', conn=c)
    assert len(rows) == 2
