# Cashbook Drill-down Modal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On `/cashbook/`, clicking a summary row (income/expense category, user tag, or month) opens a modal listing the transactions behind that figure, whose total reconciles exactly with the clicked number.

**Architecture:** One read-only JSON endpoint (`GET /cashbook/api/detail`) backed by a pure, testable helper `_get_detail_rows`, reusing the dashboard's operating scope. The dashboard template gets clickable rows + one Bootstrap modal filled by a small vanilla-JS fetch handler. No DB migration.

**Tech Stack:** Flask 3 (Python 3.9, no ORM), SQLite, Jinja2, Bootstrap 5.3 (already loaded), Chart.js not needed. pytest.

**Spec:** `docs/superpowers/specs/2026-06-09-cashbook-drilldown-design.md`

---

## Conventions & guardrails (read first)

- Run tests from `sendy_erp/`: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest ...`.
- **sendy_erp is a separate repo** — per project rules, **ask Put before any commit/push/PR** here. Do the work on a feature branch; the commit steps below run only after Put's go-ahead.
- Dev server runs `use_reloader=False` → **restart manually** (`sendy-down && sendy-up`) after editing Python/routes; verify with `curl`.
- Pre-existing red tests (NOT this work, do not "fix" here): `test_cashbook_import.py::TestEmployeeSync::test_emp001_diligence_not_clobbered`, `test_cashbook_import.py::TestRealFileImport::test_real_import_run1`, `test_cashbook_parse.py::TestRealFileIntegration::test_overview_parsed`.
- Precursor cleanup (remove ธุรกิจ-vs-ส่วนตัว section + its helper/test) is already in the working tree — this plan builds on top.

## File structure

- `inventory_app/blueprints/cashbook.py` — add `_DETAIL_DIMS`, `_get_detail_rows(conn, dim, key)`, route `detail_api`; add `jsonify` to the flask import.
- `inventory_app/templates/cashbook/dashboard.html` — make 4 tables' rows clickable; add the detail modal markup; add the fetch/render JS in `{% block scripts %}`.
- `tests/test_cashbook_detail.py` — new: helper reconciliation/scope tests + route tests.

---

## Task 0: Feature branch

- [ ] **Step 1: Fetch + branch** (run after Put approves starting)

```bash
cd /Users/putty/Sendai-Boonsawat/sendy_erp
git fetch origin
git checkout -b cashbook-drilldown origin/main
```
Expected: new branch off latest origin/main. (If precursor edits are uncommitted in the working tree, carry them along — `git status` should show the cashbook.py / dashboard.html / test_cashbook_pnl.py edits + the new spec/plan docs.)

---

## Task 1: Backend helper `_get_detail_rows`

**Files:**
- Modify: `inventory_app/blueprints/cashbook.py` (add after the existing `_get_monthly_summary`/`_get_category_summary`/`_get_tag_summary` helpers, before `# ── Routes ──`)
- Test: `tests/test_cashbook_detail.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cashbook_detail.py`:

```python
"""Unit tests for cashbook drill-down detail (_get_detail_rows).

Reconciliation property: for every dimension, the detail total equals the
matching dashboard summary helper's figure, at full float precision.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from blueprints.cashbook import (
    _get_detail_rows, _get_category_summary, _get_tag_summary,
    _get_monthly_summary,
)


def _seed(conn):
    conn.execute("DELETE FROM cashbook_transactions")
    conn.execute("DELETE FROM cashbook_accounts")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (1,'OP',1,0,1)")
    conn.execute("INSERT INTO cashbook_accounts (id, code, is_active, is_transfer, sort_order) VALUES (2,'TR',1,1,2)")
    rows = [
        (1, '2026-01-01', 'income',  'ยอดขายของ',  None,         100.0),
        (1, '2026-01-02', 'income',  'เงินทุน/เงินโอน', None,     1000.0),  # transfer cat (excluded)
        (1, '2026-01-07', 'income',  None,         None,           7.0),   # NULL category -> (ไม่ระบุ)
        (1, '2026-01-03', 'expense', 'ค่าไฟ',      'โกดัง Lion',   50.0),
        (1, '2026-01-04', 'expense', 'ค่าไฟ',      'บ้านสุนทร',    30.0),
        (1, '2026-01-05', 'expense', 'เงินทุน/เงินโอน', None,      500.0),  # transfer cat (excluded)
        (1, '2026-01-06', 'expense', 'ซื้อสินค้า',  None,          20.0),
        (2, '2026-01-01', 'income',  'ยอดขายของ',  None,         999.0),   # transfer ACCOUNT (excluded)
    ]
    conn.executemany(
        "INSERT INTO cashbook_transactions "
        "(account_id, txn_date, direction, category, user_category, amount) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def test_income_category_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    inc, _ = _get_category_summary(empty_db_conn)
    assert inc, "expected at least one income category"
    for c in inc:
        rows, summary = _get_detail_rows(empty_db_conn, 'income_category', c['category'])
        assert summary['total'] == c['total']
        assert sum(r['amount'] for r in rows) == c['total']


def test_expense_category_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    _, exp = _get_category_summary(empty_db_conn)
    assert exp
    for c in exp:
        rows, summary = _get_detail_rows(empty_db_conn, 'expense_category', c['category'])
        assert summary['total'] == c['total']
        assert sum(r['amount'] for r in rows) == c['total']


def test_user_tag_reconciles_each_row(empty_db_conn):
    _seed(empty_db_conn)
    for t in _get_tag_summary(empty_db_conn):
        rows, summary = _get_detail_rows(empty_db_conn, 'user_tag', t['tag'])
        assert summary['total'] == t['total']
        assert sum(r['amount'] for r in rows) == t['total']


def test_month_reconciles_income_and_expense(empty_db_conn):
    _seed(empty_db_conn)
    for m in _get_monthly_summary(empty_db_conn, exclude_transfer=True):
        rows, summary = _get_detail_rows(empty_db_conn, 'month', m['month'])
        assert summary['income'] == m['income']
        assert summary['expense'] == m['expense']
        assert summary['count'] == len(rows)


def test_transfer_category_never_reachable(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'expense_category', 'เงินทุน/เงินโอน')
    assert rows == []
    assert summary['total'] == 0


def test_transfer_account_excluded(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'income_category', 'ยอดขายของ')
    assert all(r['account_code'] == 'OP' for r in rows)   # TR's 999 excluded
    assert summary['total'] == 100.0


def test_unspecified_category_roundtrip(empty_db_conn):
    _seed(empty_db_conn)
    rows, summary = _get_detail_rows(empty_db_conn, 'income_category', '(ไม่ระบุ)')
    assert summary['total'] == 7.0
    assert len(rows) == 1


def test_rows_have_display_string(empty_db_conn):
    _seed(empty_db_conn)
    rows, _ = _get_detail_rows(empty_db_conn, 'expense_category', 'ค่าไฟ')
    assert rows[0]['amount_display'].startswith('฿')


def test_unknown_dim_raises(empty_db_conn):
    _seed(empty_db_conn)
    with pytest.raises(ValueError):
        _get_detail_rows(empty_db_conn, 'bogus', 'x')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_detail.py -q`
Expected: FAIL — `ImportError: cannot import name '_get_detail_rows'`.

- [ ] **Step 3: Implement `_get_detail_rows`**

In `inventory_app/blueprints/cashbook.py`, add immediately before the `# ── Routes ──` separator:

```python
# ── Drill-down detail ──────────────────────────────────────────────────────────

_DETAIL_DIMS = ("income_category", "expense_category", "user_tag", "month")


def _get_detail_rows(conn, dim, key):
    """Transactions behind a dashboard summary figure, in the SAME operating
    scope as the dashboard (transfer accounts + transfer categories excluded).

    Returns (rows, summary). Raises ValueError on an unknown dim.
    """
    if dim not in _DETAIL_DIMS:
        raise ValueError(f"unknown detail dim: {dim!r}")

    ph, params = _tcat_ph()
    where = ""
    if dim == "income_category":
        where = " AND t.direction='income' AND COALESCE(t.category,'(ไม่ระบุ)') = ?"
        params = params + [key]
    elif dim == "expense_category":
        where = " AND t.direction='expense' AND COALESCE(t.category,'(ไม่ระบุ)') = ?"
        params = params + [key]
    elif dim == "user_tag":
        where = " AND t.direction='expense' AND t.user_category = ?"
        params = params + [key]
    else:  # month
        where = " AND strftime('%Y-%m', t.txn_date) = ?"
        params = params + [key]

    sql_rows = conn.execute(f"""
        SELECT t.txn_date, a.code AS account_code, a.account_owner_name,
               t.direction, t.category, t.user_category, t.amount, t.note
        FROM cashbook_transactions t
        JOIN cashbook_accounts a ON a.id = t.account_id
        WHERE a.is_transfer = 0
          AND COALESCE(t.category,'') NOT IN ({ph})
          {where}
        ORDER BY t.txn_date DESC, t.id DESC
    """, params).fetchall()

    rows = []
    for r in sql_rows:
        d = dict(r)
        d["amount_display"] = _fmt_baht(d["amount"])
        rows.append(d)

    if dim == "month":
        income = sum(r["amount"] for r in rows if r["direction"] == "income")
        expense = sum(r["amount"] for r in rows if r["direction"] == "expense")
        summary = {
            "count": len(rows),
            "income": income, "income_display": _fmt_baht(income),
            "expense": expense, "expense_display": _fmt_baht(expense),
        }
    else:
        total = sum(r["amount"] for r in rows)
        summary = {"count": len(rows), "total": total, "total_display": _fmt_baht(total)}

    return rows, summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_detail.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit** (after Put's go-ahead)

```bash
git add tests/test_cashbook_detail.py inventory_app/blueprints/cashbook.py
git commit -m "feat(cashbook): _get_detail_rows drill-down helper + reconciliation tests"
```

---

## Task 2: Backend route `detail_api`

**Files:**
- Modify: `inventory_app/blueprints/cashbook.py` (flask import line near top; add route after the `dashboard` route)
- Test: `tests/test_cashbook_detail.py` (append route tests)

- [ ] **Step 1: Write the failing route tests**

Append to `tests/test_cashbook_detail.py`:

```python
@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def anon_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app.test_client()


def test_detail_api_valid_dim_returns_json(admin_client):
    r = admin_client.get('/cashbook/api/detail?dim=expense_category&key=ค่าไฟ')
    assert r.status_code == 200
    data = r.get_json()
    assert 'rows' in data and 'summary' in data
    assert 'count' in data['summary']
    assert data['dim'] == 'expense_category'


def test_detail_api_unknown_dim_400(admin_client):
    assert admin_client.get('/cashbook/api/detail?dim=bogus&key=x').status_code == 400


def test_detail_api_missing_key_400(admin_client):
    assert admin_client.get('/cashbook/api/detail?dim=month').status_code == 400


def test_detail_api_requires_login(anon_client):
    r = anon_client.get('/cashbook/api/detail?dim=month&key=2026-01')
    assert r.status_code in (301, 302)   # before_request redirects anon to login
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_detail.py -k detail_api -q`
Expected: FAIL — 404 (route not registered) on the valid/400 cases.

- [ ] **Step 3a: Add `jsonify` to the flask import**

In `inventory_app/blueprints/cashbook.py`, change:

```python
from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for, make_response)
```
to:
```python
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for, make_response)
```

- [ ] **Step 3b: Add the route**

In `inventory_app/blueprints/cashbook.py`, immediately after the `dashboard()` route function (before `account_ledger`):

```python
@bp_cashbook.route("/api/detail")
def detail_api():
    dim = request.args.get("dim", "")
    key = request.args.get("key", "")
    if dim not in _DETAIL_DIMS or key == "":
        abort(400)
    conn = database.get_connection()
    try:
        rows, summary = _get_detail_rows(conn, dim, key)
    finally:
        conn.close()
    return jsonify(rows=rows, summary=summary, dim=dim, key=key)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_detail.py -q`
Expected: PASS (13 passed).

- [ ] **Step 5: Restart server + smoke-check the endpoint**

```bash
sendy-down && sendy-up && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:5001/cashbook/api/detail?dim=month&key=2026-05"
```
Expected: `302` (anon via curl redirects to login — proves the route exists and is registered, not 404/500).

- [ ] **Step 6: Commit** (after Put's go-ahead)

```bash
git add tests/test_cashbook_detail.py inventory_app/blueprints/cashbook.py
git commit -m "feat(cashbook): GET /cashbook/api/detail drill-down endpoint"
```

---

## Task 3: Frontend — clickable rows + modal + JS

**Files:**
- Modify: `inventory_app/templates/cashbook/dashboard.html`

No unit test (Jinja/JS); verified via the route smoke-check above + the render test in Task 4 + a manual browser click by Put.

- [ ] **Step 1: Make the income-category rows clickable**

In `dashboard.html`, the `รายรับตามหมวดหมู่` loop — change:
```html
            {% for c in income_cats %}
            <tr>
              <td>{{ c.category }}</td>
              <td class="text-end text-success small">฿{{ "{:,.2f}".format(c.total) }}</td>
            </tr>
```
to:
```html
            {% for c in income_cats %}
            <tr data-cb-dim="income_category" data-cb-key="{{ c.category }}"
                data-cb-label="รายรับ: {{ c.category }}" role="button" style="cursor:pointer">
              <td>{{ c.category }} <i class="bi bi-chevron-right text-muted small" aria-hidden="true"></i></td>
              <td class="text-end text-success small">฿{{ "{:,.2f}".format(c.total) }}</td>
            </tr>
```

- [ ] **Step 2: Make the expense-category rows clickable**

In the `รายจ่ายตามหมวดหมู่` loop — change:
```html
            {% for c in expense_cats %}
            <tr>
              <td>{{ c.category }}</td>
              <td class="text-end text-danger small">฿{{ "{:,.2f}".format(c.total) }}</td>
            </tr>
```
to:
```html
            {% for c in expense_cats %}
            <tr data-cb-dim="expense_category" data-cb-key="{{ c.category }}"
                data-cb-label="รายจ่าย: {{ c.category }}" role="button" style="cursor:pointer">
              <td>{{ c.category }} <i class="bi bi-chevron-right text-muted small" aria-hidden="true"></i></td>
              <td class="text-end text-danger small">฿{{ "{:,.2f}".format(c.total) }}</td>
            </tr>
```

- [ ] **Step 3: Make the by-tag rows clickable**

In the `ค่าใช้จ่ายตามผู้ใช้/สถานที่` loop — change the opening `<tr>`:
```html
        {% for t in tag_summary %}
        <tr>
          <td style="min-width:160px;">
```
to:
```html
        {% for t in tag_summary %}
        <tr data-cb-dim="user_tag" data-cb-key="{{ t.tag }}"
            data-cb-label="ผู้ใช้/สถานที่: {{ t.tag }}" role="button" style="cursor:pointer">
          <td style="min-width:160px;">
```
(The existing progress-bar / amount / count `<td>`s are unchanged.)

- [ ] **Step 4: Make the monthly rows clickable**

In the `สรุปรายเดือน` loop — change:
```html
        {% for m in monthly %}
        {% set bal = m.income - m.expense %}
        <tr>
          <td class="small">{{ m.month }}</td>
```
to:
```html
        {% for m in monthly %}
        {% set bal = m.income - m.expense %}
        <tr data-cb-dim="month" data-cb-key="{{ m.month }}"
            data-cb-label="เดือน {{ m.month }}" role="button" style="cursor:pointer">
          <td class="small">{{ m.month }} <i class="bi bi-chevron-right text-muted small" aria-hidden="true"></i></td>
```

- [ ] **Step 5: Add the modal markup**

In `dashboard.html`, just before the final `{% endblock %}` of the content block (after the monthly card), add:

```html
{# ── Drill-down detail modal ───────────────────────────────────────────────── #}
<div class="modal fade" id="cbDetailModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <div>
          <h2 class="modal-title h5 mb-0" id="cbDetailTitle">รายละเอียด</h2>
          <div class="text-subtle small" id="cbDetailSummary"></div>
        </div>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="ปิด"></button>
      </div>
      <div class="modal-body">
        <div id="cbDetailLoading" class="text-center text-muted py-4 d-none">
          <div class="spinner-border spinner-border-sm" role="status"></div> กำลังโหลด...
        </div>
        <div id="cbDetailError" class="alert alert-danger d-none" role="alert"></div>
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0" id="cbDetailTable">
            <thead class="table-light">
              <tr>
                <th>วันที่</th><th>บัญชี</th><th>รับ/จ่าย</th>
                <th>หมวด</th><th>ป้าย</th>
                <th class="text-end">จำนวน</th><th>โน้ต</th>
              </tr>
            </thead>
            <tbody id="cbDetailBody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 6: Add the fetch/render JS**

At the very end of `dashboard.html` (after the content `{% endblock %}`), add a scripts block:

```html
{% block scripts %}
<script>
(function () {
  const modalEl = document.getElementById('cbDetailModal');
  if (!modalEl) return;
  const modal   = new bootstrap.Modal(modalEl);
  const titleEl = document.getElementById('cbDetailTitle');
  const sumEl   = document.getElementById('cbDetailSummary');
  const bodyEl  = document.getElementById('cbDetailBody');
  const loadEl  = document.getElementById('cbDetailLoading');
  const errEl   = document.getElementById('cbDetailError');
  const tableEl = document.getElementById('cbDetailTable');

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }

  document.querySelectorAll('[data-cb-dim]').forEach(row => {
    row.addEventListener('click', () => {
      const dim   = row.getAttribute('data-cb-dim');
      const key   = row.getAttribute('data-cb-key');
      const label = row.getAttribute('data-cb-label') || key;
      titleEl.textContent = label;
      sumEl.textContent   = '';
      bodyEl.innerHTML    = '';
      errEl.classList.add('d-none');
      tableEl.classList.add('d-none');
      loadEl.classList.remove('d-none');
      modal.show();

      fetch('{{ url_for("cashbook.detail_api") }}?dim=' +
            encodeURIComponent(dim) + '&key=' + encodeURIComponent(key))
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(data => {
          loadEl.classList.add('d-none');
          const s = data.summary;
          sumEl.textContent = (dim === 'month')
            ? 'รับ ' + s.income_display + ' · จ่าย ' + s.expense_display + ' · ' + s.count + ' รายการ'
            : 'รวม ' + s.total_display + ' · ' + s.count + ' รายการ';
          bodyEl.innerHTML = data.rows.map(r => {
            const badge = r.direction === 'income'
              ? '<span class="badge bg-success">รับ</span>'
              : '<span class="badge bg-danger">จ่าย</span>';
            return '<tr>' +
              '<td class="small">' + esc(r.txn_date) + '</td>' +
              '<td class="small">' + esc(r.account_code) + '</td>' +
              '<td>' + badge + '</td>' +
              '<td class="small">' + esc(r.category) + '</td>' +
              '<td class="small">' + esc(r.user_category) + '</td>' +
              '<td class="text-end small">' + esc(r.amount_display) + '</td>' +
              '<td class="small text-muted">' + esc(r.note) + '</td>' +
            '</tr>';
          }).join('') || '<tr><td colspan="7" class="text-muted small">ไม่มีรายการ</td></tr>';
          tableEl.classList.remove('d-none');
        })
        .catch(err => {
          loadEl.classList.add('d-none');
          errEl.textContent = 'โหลดข้อมูลไม่สำเร็จ: ' + err.message;
          errEl.classList.remove('d-none');
        });
    });
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 7: Restart + render smoke-check**

```bash
sendy-down && sendy-up && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/cashbook/
```
Expected: `302` (anon redirect; not 500). The render test in Task 4 exercises the authed template.

- [ ] **Step 8: Commit** (after Put's go-ahead)

```bash
git add inventory_app/templates/cashbook/dashboard.html
git commit -m "feat(cashbook): clickable summary rows + drill-down detail modal"
```

---

## Task 4: Full verification

- [ ] **Step 1: Add a render assertion for the modal**

In `tests/test_cashbook_detail.py`, append:

```python
def test_dashboard_includes_drilldown_modal(admin_client):
    html = admin_client.get('/cashbook/').data.decode('utf-8')
    assert 'id="cbDetailModal"' in html
    assert 'data-cb-dim="month"' in html
    assert 'data-cb-dim="expense_category"' in html
```

- [ ] **Step 2: Run the cashbook suite**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_detail.py tests/test_cashbook_pnl.py tests/test_bp_cashbook_routes.py -q`
Expected: PASS (all green; 14 in test_cashbook_detail.py).

- [ ] **Step 3: Run the full suite (confirm no new breakage)**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -q`
Expected: only the 3 known pre-existing failures listed in "guardrails" remain red; everything else green.

- [ ] **Step 4: Manual browser check (Put)**

Restart sendy, open `http://127.0.0.1:5001/cashbook/`, click a row in each of the 4 tables; confirm the modal opens, lists transactions, and the header total matches the clicked number. For a month, confirm รับ/จ่าย match the row's two columns.

- [ ] **Step 5: Commit the render test** (after Put's go-ahead)

```bash
git add tests/test_cashbook_detail.py
git commit -m "test(cashbook): assert drill-down modal renders on dashboard"
```

- [ ] **Step 6: PR** (only on Put's explicit go-ahead — outward-facing)

```bash
git push -u origin cashbook-drilldown
gh pr create --base main --title "Cashbook: drill-down detail modal + drop biz/personal split" \
  --body "Click an income/expense category, user tag, or month on /cashbook/ to see the transactions behind it (reconciling total). Also removes the ธุรกิจ-vs-ส่วนตัว section (all cashbook expenses are business). No migration."
```

---

## Self-review notes

- **Spec coverage:** 4 dims (Task 3 rows + Task 1 helper), scope filters (Task 1), endpoint contract (Task 2), modal UX incl. loading/error (Task 3), reconciliation property (Task 1 tests), `(ไม่ระบุ)` round-trip (Task 1), access/anon (Task 2), no migration + restart (Tasks 2/3). All covered.
- **Type consistency:** helper returns `(rows, summary)`; summary keys `count`+`total`/`total_display` (cat/tag) or `income(_display)`/`expense(_display)` (month) — used identically in route JSON and JS. Row keys `txn_date, account_code, account_owner_name, direction, category, user_category, amount, amount_display, note` — JS reads `txn_date, account_code, direction, category, user_category, amount_display, note` (subset; consistent).
- **No placeholders:** every code/command step is concrete.
