# Customer-Credit-Balance Section on `/cashflow` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only "ยอดเครดิตลูกค้าค้างคืน" section to `/cashflow` listing per-invoice rows where the customer overpaid (`outstanding < 0`), filtered to ≥฿5 by default with a "แสดงทั้งหมด" toggle.

**Architecture:** New pure helper `payments_alloc.customer_credit_rows()` wraps `invoice_settlement()` and returns sorted overpaid rows. `cashflow_dashboard()` calls the helper and passes data to `cashflow.html`, which renders a new card under the AR aging section. No DB schema change. No money-path change.

**Tech Stack:** Python 3.9, Flask 3.x (no ORM), SQLite, Jinja2, pytest. Tests follow the established `empty_db_conn` synthetic-data and `tmp_db` route-test patterns in `sendy_erp/tests/`.

**Spec:** [`../specs/2026-05-20-customer-credit-balance-design.md`](../specs/2026-05-20-customer-credit-balance-design.md)

**Commit policy (user instruction):** ONE final commit at the end bundling spec + plan + code + tests. Do not commit between tasks.

---

## Task 0: Branch setup

**Files:**
- None modified yet; just git state.

- [ ] **Step 1: Fetch + confirm clean main**

```bash
cd ~/Sendai-Boonsawat/sendy_erp
git fetch origin
git status -sb
```

Expected: `## main...origin/main` and no working-tree changes (the docs/specs and docs/plans files created during brainstorming are the only untracked items so far — they will be part of the final commit, leave them in place).

- [ ] **Step 2: Create feature branch from main**

```bash
git checkout -b feat/customer-credit-balance-section
git status -sb
```

Expected: `## feat/customer-credit-balance-section`. Untracked spec + plan files still present.

---

## Task 1: Helper `customer_credit_rows()` — TDD

**Files:**
- Create: `sendy_erp/tests/test_customer_credit_rows.py`
- Modify: `sendy_erp/inventory_app/payments_alloc.py` (append one function near the existing `customer_outstanding`)

- [ ] **Step 1: Write the failing test file**

Create `sendy_erp/tests/test_customer_credit_rows.py` with this exact content. The synthetic data builders mirror `tests/test_payments_alloc.py` so the file is self-contained and won't break if that file's helpers move.

```python
"""TDD tests for payments_alloc.customer_credit_rows().

Synthetic-data only, on empty_db_conn schema clone. Mirrors the
sales_transactions/received_payments/paid_invoices idiom already
established in tests/test_payments_alloc.py.
"""
from datetime import date, timedelta

import pytest

import payments_alloc as pa


# ── synthetic data builders ──────────────────────────────────────────────────

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


def _overpay(conn, doc_base, customer, billed, paid, date_iso='2026-05-10',
             customer_code='C1'):
    """Convenience: one billed invoice + one receipt that pays MORE than
    billed, leaving credit = paid - billed."""
    _ins_sale(conn, doc_base, customer, customer_code, date_iso, billed,
              vat_type=1)
    re_id = _ins_receipt(conn, f"RE-{doc_base}", customer, date_iso,
                         total=paid)
    _ins_paid(conn, re_id, doc_base, paid)


# ── tests ────────────────────────────────────────────────────────────────────

def test_basic_overpaid_invoice_appears(empty_db_conn):
    _overpay(empty_db_conn, 'IV001', 'Acme', billed=100.0, paid=150.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert len(rows) == 1
    r = rows[0]
    assert r['doc_base'] == 'IV001'
    assert r['customer'] == 'Acme'
    assert r['credit'] == 50.0
    assert r['credit'] > 0   # invariant: never expose a signed/negative credit


def test_eps_clamp_excludes_near_zero_credit(empty_db_conn):
    # _EPS = 0.005; an over-collection of 0.003 (sub-cent) must be clamped
    # away by _reconcile and therefore must not appear at any threshold.
    _overpay(empty_db_conn, 'IV002', 'Rounding', billed=100.0, paid=100.003)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows == []


def test_filters_below_threshold(empty_db_conn):
    # Two overpaid invoices: ฿2 (noise) and ฿10 (material).
    _overpay(empty_db_conn, 'IV003', 'A', billed=100.0, paid=102.0)
    _overpay(empty_db_conn, 'IV004', 'B', billed=100.0, paid=110.0)
    rows = pa.customer_credit_rows(threshold=5.0, conn=empty_db_conn)
    assert [r['doc_base'] for r in rows] == ['IV004']


def test_show_all_threshold_zero_includes_everything_above_eps(empty_db_conn):
    _overpay(empty_db_conn, 'IV005', 'A', billed=100.0, paid=102.0)
    _overpay(empty_db_conn, 'IV006', 'B', billed=100.0, paid=110.0)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert sorted(r['doc_base'] for r in rows) == ['IV005', 'IV006']


def test_sort_credit_desc_then_invoice_date_desc(empty_db_conn):
    # Same credit (฿20), different dates — newer first.
    _overpay(empty_db_conn, 'IV007', 'A', billed=100.0, paid=120.0,
             date_iso='2026-01-15')
    _overpay(empty_db_conn, 'IV008', 'B', billed=100.0, paid=120.0,
             date_iso='2026-04-15')
    # Bigger credit always wins regardless of date.
    _overpay(empty_db_conn, 'IV009', 'C', billed=100.0, paid=200.0,
             date_iso='2025-12-01')
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert [r['doc_base'] for r in rows] == ['IV009', 'IV008', 'IV007']


def test_empty_when_no_overpaid(empty_db_conn):
    # Underpaid invoice — must NOT surface here.
    _ins_sale(empty_db_conn, 'IV010', 'A', 'C1', '2026-05-01', 100.0)
    re_id = _ins_receipt(empty_db_conn, 'RE-IV010', 'A', '2026-05-01',
                         total=80.0)
    _ins_paid(empty_db_conn, re_id, 'IV010', 80.0)
    assert pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn) == []


def test_days_old_computed_from_today(empty_db_conn):
    # Invoice 30 days old → days_old == 30.
    invoice_date = (date.today() - timedelta(days=30)).isoformat()
    _overpay(empty_db_conn, 'IV011', 'A', billed=100.0, paid=120.0,
             date_iso=invoice_date)
    rows = pa.customer_credit_rows(threshold=0.0, conn=empty_db_conn)
    assert rows[0]['days_old'] == 30
```

- [ ] **Step 2: Run the new test file — expect 7 failures**

```bash
cd ~/Sendai-Boonsawat/sendy_erp
~/.virtualenvs/erp/bin/pytest tests/test_customer_credit_rows.py -v
```

Expected: 7 errors with `AttributeError: module 'payments_alloc' has no attribute 'customer_credit_rows'`.

- [ ] **Step 3: Implement `customer_credit_rows()`**

Append this function to `sendy_erp/inventory_app/payments_alloc.py`, directly after `customer_outstanding()` (around line 640). Match the existing module style (snake_case, `Optional[sqlite3.Connection]`, dict returns).

```python
def customer_credit_rows(threshold: float = 5.0,
                         conn: Optional[sqlite3.Connection] = None,
                         db_path: Optional[str] = None) -> list[dict]:
    """Per-invoice list where the customer overpaid (outstanding < 0).

    Filters `invoice_settlement()` for invoices whose reconciled
    `outstanding` is strictly negative AND |outstanding| >= threshold.
    Pass threshold=0 to surface everything down to the _EPS clamp.

    Sort: credit DESC, then invoice_date DESC (newer first).

    Returns dicts with keys:
        doc_base, customer, customer_code, invoice_date,
        billed, credit_notes, collected,
        credit            # = -outstanding, always > 0 in this list
        days_old          # int, today - invoice_date
    """
    from datetime import date as _date

    settled = invoice_settlement(conn=conn, db_path=db_path)
    today_iso = _date.today().isoformat()

    out: list[dict] = []
    for r in settled:
        outstanding = r['outstanding']
        if outstanding >= 0:
            continue  # not overpaid
        credit = round(-outstanding, 2)
        if credit < threshold:
            continue

        # days_old: tolerant of missing invoice_date (legacy rows)
        days_old: Optional[int] = None
        inv_date = r.get('invoice_date')
        if inv_date:
            try:
                y, m, d = (int(x) for x in inv_date.split('-')[:3])
                days_old = (_date.today() - _date(y, m, d)).days
            except (ValueError, TypeError):
                days_old = None

        out.append({
            'doc_base':      r['doc_base'],
            'customer':      r['customer'],
            'customer_code': r['customer_code'],
            'invoice_date':  inv_date,
            'billed':        r['billed'],
            'credit_notes':  r['credit_notes'],
            'collected':     r['collected'],
            'credit':        credit,
            'days_old':      days_old,
        })

    # Stable sort: sort by least-significant key first, then by primary.
    out.sort(key=lambda x: x['invoice_date'] or '', reverse=True)
    out.sort(key=lambda x: x['credit'], reverse=True)
    return out
```

Note: `Optional` is already imported at the top of `payments_alloc.py` (verified: `from typing import Optional` at line 86) — no new imports needed.

- [ ] **Step 4: Run the new test file — expect 7 passes**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_customer_credit_rows.py -v
```

Expected: `7 passed`.

- [ ] **Step 5: Run the full pytest suite to confirm no regression**

```bash
~/.virtualenvs/erp/bin/pytest
```

Expected: full suite passes (baseline was 392 + 7 new = 399 pass; may differ if other branches landed tests in the meantime).

---

## Task 2: Route + template — TDD

**Files:**
- Create: `sendy_erp/tests/test_cashflow_route.py`
- Modify: `sendy_erp/inventory_app/app.py:3176-3239` (function `cashflow_dashboard`)
- Modify: `sendy_erp/inventory_app/templates/cashflow.html`

- [ ] **Step 1: Write the failing route test**

Create `sendy_erp/tests/test_cashflow_route.py` with this exact content. Mirrors the established `test_revenue_route.py` pattern (SKIP_DB_INIT + tmp_db + route_db fixture + admin client).

```python
"""Happy-path integration tests for the /cashflow customer-credit-balance
section.

Uses tmp_db so route + cashflow.py + payments_alloc.py + template all run
against a real schema copy. Guards against template-variable rename
regressions that unit tests miss.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def route_db(tmp_db, monkeypatch):
    """Patch DATABASE_PATH on modules that captured it via
    `from config import DATABASE_PATH` at import time."""
    import payments_alloc, cashflow
    for mod in (payments_alloc, cashflow):
        monkeypatch.setattr(mod, 'DATABASE_PATH', tmp_db, raising=True)
    return tmp_db


def _client_as_admin():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'test-admin'
    return c


def _client_as_staff():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'staff'
        s['username'] = 'test-staff'
    return c


def test_cashflow_route_renders_credit_section_header(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'ยอดเครดิตลูกค้าค้างคืน' in body, (
        "Expected the new credit-balance section header to render in "
        "/cashflow output but it was not found."
    )


def test_cashflow_route_default_filter_offers_show_all_link(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    # Default-filter view has the show-all toggle visible.
    assert 'show_all=1' in body, (
        "Expected ?show_all=1 toggle link in default-filter view."
    )


def test_cashflow_route_show_all_offers_hide_link(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow?show_all=1')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # show_all view replaces the toggle text with the inverse.
    assert 'ซ่อนรายการต่ำกว่า' in body, (
        "Expected the 'hide low-value rows' link when show_all=1 is on."
    )


def test_cashflow_route_staff_role_blocked(route_db):
    c = _client_as_staff()
    resp = c.get('/cashflow', follow_redirects=False)
    # Existing admin/manager gate — must still 302 to /, never leaking
    # the new section.
    assert resp.status_code == 302
```

- [ ] **Step 2: Run the new route test — expect 4 failures (or 3 + 1 pass on the staff-blocked test which existed behavior already)**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_cashflow_route.py -v
```

Expected:
- `test_cashflow_route_staff_role_blocked` PASS (existing gate already redirects).
- The other 3 FAIL — the section header / show-all link / hide-link strings are not yet in the template.

- [ ] **Step 3: Wire the helper into `cashflow_dashboard()`**

Open `sendy_erp/inventory_app/app.py`. Locate `cashflow_dashboard()` (currently `@app.route('/cashflow')` starting at line 3176). Apply these two edits:

**Edit A** — add the helper module alias near the top of the route (after `cf_mod` is already in scope):

Locate this existing block (around lines 3217-3219):

```python
    cash_rows   = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    aging       = cf_mod.ar_aging()          # always point-in-time today
    revenue_rows = cf_mod.revenue_by_month(date_from=date_from, date_to=date_to)
```

Replace with:

```python
    cash_rows   = cf_mod.cash_in_by_month(date_from=date_from, date_to=date_to)
    aging       = cf_mod.ar_aging()          # always point-in-time today
    revenue_rows = cf_mod.revenue_by_month(date_from=date_from, date_to=date_to)

    # Customer-credit-balance section (point-in-time today, not period).
    show_all_credit  = request.args.get('show_all') in ('1', 'true', 'on')
    credit_threshold = 0.0 if show_all_credit else 5.0
    credit_rows      = pa_mod.customer_credit_rows(threshold=credit_threshold)
    credit_total     = round(sum(r['credit'] for r in credit_rows), 2)
    credit_hidden_count = (
        len(pa_mod.customer_credit_rows(threshold=0.0)) - len(credit_rows)
    )
```

**Edit B** — extend the `render_template(...)` call (around lines 3226-3239) with the five new keyword arguments. Locate:

```python
    return render_template(
        'cashflow.html',
        cash_rows=cash_rows,
        aging=aging,
        revenue_rows=revenue_rows,
        total_cash_in=total_cash_in,
        total_receipts=total_receipts,
        total_outstanding=total_outstanding,
        total_open_count=total_open_count,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
    )
```

Add five lines just before the closing `)`:

```python
    return render_template(
        'cashflow.html',
        cash_rows=cash_rows,
        aging=aging,
        revenue_rows=revenue_rows,
        total_cash_in=total_cash_in,
        total_receipts=total_receipts,
        total_outstanding=total_outstanding,
        total_open_count=total_open_count,
        from_month=from_month,
        to_month=to_month,
        date_from=date_from,
        date_to=date_to,
        credit_rows=credit_rows,
        credit_total=credit_total,
        credit_threshold=credit_threshold,
        credit_hidden_count=credit_hidden_count,
        show_all_credit=show_all_credit,
    )
```

**Edit C** — add the `payments_alloc` import (verified during plan-write that it is NOT yet imported in `app.py`; only `cashflow as cf_mod` and `revenue as rev_mod` are present at lines 29-30).

Open `inventory_app/app.py`. Locate the existing module imports block around lines 29-30:

```python
import cashflow as cf_mod
import revenue as rev_mod
```

Append one line below:

```python
import cashflow as cf_mod
import revenue as rev_mod
import payments_alloc as pa_mod
```

That alias (`pa_mod`) is what the two `customer_credit_rows(...)` call sites in Edit A use.

- [ ] **Step 4: Add the new section to `cashflow.html`**

Open `sendy_erp/inventory_app/templates/cashflow.html`. Locate the existing AR-aging card (`{% set aging_buckets = aging.buckets %}` or similar — search for `total_outstanding`). Immediately **after** that card's closing `</div>` (and **before** the revenue section), insert this Jinja block:

```jinja
{# ── Customer-credit-balance (overpaid invoices) ───────────────────────── #}
<div class="card mt-4">
  <div class="card-header d-flex justify-content-between align-items-center">
    <div>
      <h5 class="mb-0">ยอดเครดิตลูกค้าค้างคืน</h5>
      <small class="text-muted">บิลที่ลูกค้าจ่ายเกินยอดสุทธิ — เราเป็นหนี้ลูกค้า</small>
    </div>
    <div class="text-end">
      <div class="fw-bold text-danger">฿{{ '{:,.2f}'.format(credit_total) }}</div>
      <small class="text-muted">{{ credit_rows|length }} รายการ</small>
    </div>
  </div>
  <div class="card-body p-0">
    {% if credit_rows %}
    <div class="table-responsive">
      <table class="table table-sm mb-0 align-middle">
        <thead class="table-light">
          <tr>
            <th>IV</th>
            <th>ลูกค้า</th>
            <th>วันที่บิล</th>
            <th class="text-end">ยอดบิล</th>
            <th class="text-end">ใบลดหนี้</th>
            <th class="text-end">รับชำระ</th>
            <th class="text-end">ยอดเครดิต</th>
            <th class="text-end">อายุ</th>
          </tr>
        </thead>
        <tbody>
          {% for r in credit_rows %}
          <tr>
            <td>
              <a href="{{ url_for('sales_doc', doc_base=r.doc_base) }}">
                {{ r.doc_base }}
              </a>
            </td>
            <td>
              <a href="{{ url_for('customer_summary', customer_name=r.customer) }}">
                {{ r.customer }}
              </a>
            </td>
            <td>{{ r.invoice_date or '—' }}</td>
            <td class="text-end">฿{{ '{:,.2f}'.format(r.billed) }}</td>
            <td class="text-end">
              {% if r.credit_notes %}฿{{ '{:,.2f}'.format(r.credit_notes) }}{% else %}—{% endif %}
            </td>
            <td class="text-end">฿{{ '{:,.2f}'.format(r.collected) }}</td>
            <td class="text-end fw-bold text-danger">
              ฿{{ '{:,.2f}'.format(r.credit) }}
            </td>
            <td class="text-end">
              {% if r.days_old is not none %}{{ r.days_old }} วัน{% else %}—{% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div class="p-3 text-center text-muted">
      ไม่มียอดเครดิตค้างคืน
      {% if not show_all_credit %}(เกณฑ์ ≥฿5){% endif %}
    </div>
    {% endif %}
  </div>
  <div class="card-footer">
    {% if show_all_credit %}
      <a href="{{ url_for('cashflow_dashboard', **{'from': from_month, 'to': to_month}) }}">
        ซ่อนรายการต่ำกว่า ฿5 (rounding noise)
      </a>
    {% else %}
      {% if credit_hidden_count > 0 %}
        ซ่อน {{ credit_hidden_count }} รายการต่ำกว่า ฿5 (rounding noise) —
      {% endif %}
      <a href="{{ url_for('cashflow_dashboard',
                          show_all=1,
                          **{'from': from_month, 'to': to_month}) }}">
        แสดงทั้งหมด (show_all=1)
      </a>
    {% endif %}
  </div>
</div>
```

Endpoint names verified during plan-write:
- `sales_doc(doc_base)` at `app.py:1589` → `/sales/doc/<doc_base>` (per-invoice detail).
- `customer_summary(customer_name)` at `app.py:1361` → `/customer/<customer_name>` (customer detail page).
- There is no `/payment-status/<doc_base>` route; `/payment-status` is a list view with query-string filters. The IV link therefore goes to `sales_doc`, which matches what `/accounting/ar-followup` does for per-invoice drill-in.

- [ ] **Step 5: Run the route test — expect 4 passes**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_cashflow_route.py -v
```

Expected: `4 passed`.

- [ ] **Step 6: Run the full pytest suite — expect no regressions**

```bash
~/.virtualenvs/erp/bin/pytest
```

Expected: full suite passes, count is baseline + 11 new tests (7 helper + 4 route).

---

## Task 3: Manual smoke + final commit

**Files:** None new. This task verifies the running app and bundles the final commit.

- [ ] **Step 1: Restart dev server**

```bash
sendy-down 2>/dev/null
sendy-up
sleep 2
tail -20 /tmp/sendy.log
```

Expected: `* Running on http://127.0.0.1:5001` and no traceback in the tail.

- [ ] **Step 2: Open `/cashflow` as admin in browser**

Open `http://127.0.0.1:5001/cashflow` after logging in as admin.

Expected acceptance criteria from the spec:
- A card titled **"ยอดเครดิตลูกค้าค้างคืน"** appears directly under the AR aging section.
- It lists invoices where `credit ≥ ฿5` (~3 rows on current live data: IV6800219 ฿290, เส็งหลี ฿40.61, เมืองคงวัสดุ ฿35 — exact list depends on current DB).
- Footer reads "รวม ≈฿372 (N รายการ)" and "ซ่อน K รายการต่ำกว่า ฿5 — แสดงทั้งหมด (show_all=1)".
- Clicking **แสดงทั้งหมด** navigates to `/cashflow?show_all=1` and shows all overpaid invoices.
- Clicking an IV link goes to `/payment-status/<doc_base>` for that invoice.
- Clicking a customer name link goes to `/customer/<name>`.

If any acceptance criterion fails, debug at the route or template — DO NOT skip ahead to commit.

- [ ] **Step 3: Confirm clean working tree apart from this feature**

```bash
git status -sb
```

Expected (paths approximate, in any order):

```
## feat/customer-credit-balance-section
?? docs/superpowers/plans/2026-05-20-customer-credit-balance.md
?? docs/superpowers/specs/2026-05-20-customer-credit-balance-design.md
?? tests/test_cashflow_route.py
?? tests/test_customer_credit_rows.py
 M inventory_app/app.py
 M inventory_app/payments_alloc.py
 M inventory_app/templates/cashflow.html
```

If any **other** file is modified, investigate — do not blindly commit.

- [ ] **Step 4: Stage + commit (final, single commit per user instruction)**

```bash
git add docs/superpowers/specs/2026-05-20-customer-credit-balance-design.md \
        docs/superpowers/plans/2026-05-20-customer-credit-balance.md \
        inventory_app/payments_alloc.py \
        inventory_app/app.py \
        inventory_app/templates/cashflow.html \
        tests/test_customer_credit_rows.py \
        tests/test_cashflow_route.py

git commit -m "$(cat <<'EOF'
feat(cashflow): customer credit balance section on /cashflow

Read-only section under AR aging that lists invoices where the customer
overpaid (outstanding < 0). Pure helper customer_credit_rows() wraps
invoice_settlement(); default ≥฿5 filter hides rounding noise, with a
"แสดงทั้งหมด" (?show_all=1) toggle to surface everything down to _EPS.

No schema change, no money-path change. Permission gate inherited from
/cashflow (admin/manager). 7 helper unit tests + 4 route tests added.

Spec: docs/superpowers/specs/2026-05-20-customer-credit-balance-design.md
Plan: docs/superpowers/plans/2026-05-20-customer-credit-balance.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

git status -sb
git log --oneline -1
```

Expected: a clean tree, branch `feat/customer-credit-balance-section`, one new commit at HEAD.

- [ ] **Step 5: Push branch and announce PR-readiness**

```bash
git push -u origin feat/customer-credit-balance-section
```

Then surface to Put:

> Branch pushed. Open the PR at:
> `https://github.com/Zyringe/sendy-erp/pull/new/feat/customer-credit-balance-section`
>
> Base = main. Suggested title: `feat(cashflow): customer credit balance section on /cashflow`. Body can copy from the commit message.

---

## Codex review additions (applied in same commit)

After running the plan above, Codex review (no BLOCK, 4 WARN + 2 NIT)
added these on top of the plan:

1. **`customer_credit_rows()` gained an `as_of: Optional[str] = None`
   parameter** (defaults to today) — caps future-dated invoices.
2. **Route refactored to single snapshot** — `cashflow_dashboard()` now
   calls `pa_mod.customer_credit_rows(threshold=0.0)` exactly once and
   Python-filters down to the displayed view; `credit_hidden_count`
   derives from the same snapshot.
3. **`credit_threshold` template kwarg dropped** (was unused — template
   hardcodes "฿5").
4. **5 more helper tests** in `tests/test_customer_credit_rows.py`:
   `test_sr_fully_credited_does_not_appear`,
   `test_cancelled_receipt_does_not_register_overpay`,
   `test_multiline_invoice_overpaid_emits_one_row`,
   `test_vat2_billed_uses_inclusive_amount`,
   `test_as_of_excludes_future_dated_invoice`.
5. **1 more route test** in `tests/test_cashflow_route.py`:
   `test_cashflow_route_renders_a_real_credit_row` — asserts the
   top live-DB credit row's `doc_base` + `url_for('sales_doc')` link
   actually appear in the rendered HTML.
6. **VAT clarifier** on the "ยอดบิล" column header (tooltip noting it
   is VAT-inclusive when `vat_type=2`, matching AR aging semantics).

Final suite: **405 passed**.

## Self-review notes (already applied)

- **Spec coverage:** All 6 acceptance criteria from the spec are testable
  via Task 2's 4 route tests + Task 3's manual smoke.
- **Type consistency:** `customer_credit_rows()` signature and return
  dict keys identical in test file, helper definition, route call, and
  template field access.
- **No placeholders:** Every step contains the code or command to run.
  The only "placeholder" callouts are the explicit "before saving,
  confirm endpoint names" check in Task 2 Step 4 — that is a real
  precondition, not a TODO.
- **Spec → plan cross-link:** Spec sits at `docs/superpowers/specs/…`
  and is referenced in the plan header.
