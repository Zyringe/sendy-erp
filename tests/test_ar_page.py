"""Tests for the unified /ar page (AR consolidation).

Task 1: get_ar_reconciliation() shape + totals
Task 2: /ar route + overview tab
Task 3: customers tab + access gating
Task 4: invoices tab
Task 5: reconcile tab
Task 6: redirects + access
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')


# ── Task 1 ────────────────────────────────────────────────────────────────────

def test_get_ar_reconciliation_shape_and_totals(tmp_db):
    import models
    rec = models.get_ar_reconciliation()
    assert set(rec) >= {'rows', 'snapshot_total', 'ledger_total', 'diff_total'}
    # snapshot_total must equal the snapshot AR helper sum (same source)
    snap = sum(r['outstanding_amount'] or 0 for r in models.get_customer_debt_summary())
    assert abs(rec['snapshot_total'] - snap) < 0.01
    # diff_total == ledger_total - snapshot_total
    assert abs(rec['diff_total'] - (rec['ledger_total'] - rec['snapshot_total'])) < 0.01
    # each row's diff is internally consistent and status is valid
    for r in rec['rows']:
        assert abs(r['diff'] - (r['ledger_amount'] - r['snapshot_amount'])) < 0.01
        assert r['status'] in ('match', 'diff', 'snapshot_only', 'ledger_only')


def test_reconciliation_ledger_total_matches_payment_summary(tmp_db):
    import models
    rec = models.get_ar_reconciliation()
    summ = models.get_payment_summary()
    # ledger reconcile total should be within a small tolerance of the summary unpaid
    assert abs(rec['ledger_total'] - summ['unpaid_amount']) < max(50.0, 0.02 * summ['unpaid_amount'])


# ── Task 2 helpers ────────────────────────────────────────────────────────────

def _admin(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    return c


# ── Task 2 ────────────────────────────────────────────────────────────────────

def test_ar_overview_renders_and_totals_match(tmp_db):
    import models
    c = _admin(tmp_db)
    r = c.get('/ar')                      # default tab=overview
    assert r.status_code == 200
    body = r.data.decode()
    assert 'ภาพรวม' in body and 'กระทบยอด' in body          # tab bar present
    # snapshot headline number appears (formatted with comma)
    snap = sum(x['outstanding_amount'] or 0 for x in models.get_customer_debt_summary())
    assert f"{snap:,.0f}".split('.')[0][:3] in body          # leading digits present


# ── Task 3 ────────────────────────────────────────────────────────────────────

def test_customers_tab_total_matches_and_staff_can_view(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 3; s['username'] = 'staffer'; s['role'] = 'staff'
    r = c.get('/ar?tab=customers')
    assert r.status_code == 200, 'staff must VIEW the customers tab'
    body = r.data.decode()
    # staff sees the list (read) but NOT the dunning drill-down (manager-gated
    # detail) — and is shown the view-only notice naming the right requirement.
    assert 'ดูบิล/ทวง' not in body, 'staff must not get the manager-gated drill-down link'
    assert 'ต้องสิทธิ์ Manager+' in body, 'staff should see the view-only notice'


# ── Task 4 ────────────────────────────────────────────────────────────────────

def test_invoices_tab_unpaid_count(tmp_db):
    import models
    c = _admin(tmp_db)
    r = c.get('/ar?tab=invoices')
    assert r.status_code == 200
    assert str(models.get_payment_summary()['unpaid_count']) in r.data.decode()


# ── Task 5 ────────────────────────────────────────────────────────────────────

def test_reconcile_tab_shows_both_totals(tmp_db):
    import models
    c = _admin(tmp_db)
    r = c.get('/ar?tab=reconcile')
    body = r.data.decode()
    assert r.status_code == 200
    rec = models.get_ar_reconciliation()
    assert f"{rec['snapshot_total']:,.0f}".split('.')[0][:3] in body
    assert f"{rec['ledger_total']:,.0f}".split('.')[0][:3] in body


# ── Phase 2 (finance revamp R1) ─────────────────────────────────────────────
# "/ar owns all AR" — the ยอดเครดิตลูกค้าค้างคืน (customer credit balance)
# section moves from /cashflow to /ar's reconcile tab.

def test_reconcile_tab_shows_customer_credit_section(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/ar?tab=reconcile')
    body = r.data.decode()
    assert r.status_code == 200
    assert 'ยอดเครดิตลูกค้าค้างคืน' in body, (
        "Expected the customer-credit-balance section (moved from /cashflow) "
        "to render on /ar?tab=reconcile."
    )


def test_reconcile_tab_show_all_toggle_present(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/ar?tab=reconcile')
    body = r.data.decode()
    assert 'show_all=1' in body


def test_reconcile_tab_show_all_offers_hide_link(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/ar?tab=reconcile&show_all=1')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'ซ่อนรายการต่ำกว่า' in body


# ── Phase 2 (finance revamp R3) ─────────────────────────────────────────────
# ar.html:43 used to hardcode "34 ราย" regardless of the real snapshot count.

def test_overview_snapshot_count_is_dynamic_not_hardcoded(tmp_db):
    """Insert one additional collectable customer into the live-DB clone's
    latest BSN snapshot and assert the rendered count moves accordingly — a
    hardcoded literal would never move, and would keep showing the OLD
    (pre-insert) count text no matter what the DB says."""
    import sqlite3
    import models

    before_count = len(models.get_customer_debt_summary())

    conn = sqlite3.connect(tmp_db)
    snap, batch_id = conn.execute("""
        SELECT snapshot_date_iso, batch_id FROM express_ar_outstanding
         WHERE entity = 'BSN'
         ORDER BY snapshot_date_iso DESC LIMIT 1
    """).fetchone()
    conn.execute("""
        INSERT INTO express_ar_outstanding
            (batch_id, snapshot_date_iso, customer_code, customer_name,
             doc_no, doc_date_iso, is_anomalous, bill_amount, paid_amount,
             outstanding_amount, entity)
        VALUES (?, ?, 'ZZTEST01', 'ทดสอบไดนามิก', 'IVZZTEST01', ?, 0,
                999.0, 0.0, 999.0, 'BSN')
    """, (batch_id, snap, snap))
    conn.commit()
    conn.close()

    after_count = before_count + 1

    c = _admin(tmp_db)
    r = c.get('/ar')
    body = r.data.decode()
    assert f"{after_count:,} ราย" in body, (
        f"Expected the dynamic count ({after_count} ราย) to appear after "
        "inserting an extra snapshot row."
    )
    assert f"{before_count:,} ราย" not in body, (
        "Old count text still present — the count did not update, "
        "suggesting it is still hardcoded."
    )


# ── Task 6 ────────────────────────────────────────────────────────────────────

def test_old_ar_routes_redirect_to_unified(tmp_db):
    c = _admin(tmp_db)
    for path, tab in [('/express/ar', 'overview'), ('/accounting/ar-followup', 'customers'),
                      ('/payment-status', 'invoices'), ('/payment-status/customers', 'customers')]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 302 and '/ar' in r.headers['Location'], path


def test_whitelist_and_module_keys_valid(tmp_db):
    # reuse the existing guard pattern: every _ENDPOINT_MODULE key is a real endpoint
    from app import app as a, _ENDPOINT_MODULE
    eps = {r.endpoint for r in a.url_map.iter_rules()}
    assert not (set(_ENDPOINT_MODULE) - eps)
