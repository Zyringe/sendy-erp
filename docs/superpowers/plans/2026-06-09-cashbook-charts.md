# Cashbook Charts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a monthly income/expense trend chart and an expense-by-category donut to `/cashbook/`, both clickable into the existing drill-down modal.

**Architecture:** Two Chart.js 4.4.4 charts (house pattern) above the existing tables. Trend uses the existing `monthly` data; donut uses a new pure `_expense_topn` helper over `expense_cats`. The modal-open JS is refactored into one `openCbDetail(dim, key, label)` reused by table rows and chart clicks. No migration, no new route.

**Tech Stack:** Flask/Jinja2 (Python 3.9), Chart.js 4.4.4 (CDN), Bootstrap 5.3, pytest.

**Spec:** `docs/superpowers/specs/2026-06-09-cashbook-charts-design.md`
**Branch:** `cashbook-drilldown` (current; same PR #124 — charts depend on the drill-down modal). Local commits OK (Put authorized); push/PR already open.

---

## Conventions & guardrails
- Run tests from `sendy_erp/`: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest ...`.
- Dev server `use_reloader=False` → restart (`sendy-down && sendy-up`) after edits; verify with `curl`.
- Pre-existing red tests (NOT this work): 3 cashbook-import/parse + 2 HR-payroll (all on origin/main).

## File structure
- `inventory_app/blueprints/cashbook.py` — add `_expense_topn`; pass `expense_chart` from `dashboard()`.
- `inventory_app/templates/cashbook/dashboard.html` — charts row (2 canvases); refactor modal JS → `openCbDetail`; add Chart.js CDN + init.
- `tests/test_cashbook_charts.py` — new (helper unit tests + render test).

---

## Task 1: `_expense_topn` helper + route wiring

**Files:**
- Modify: `inventory_app/blueprints/cashbook.py`
- Test: `tests/test_cashbook_charts.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cashbook_charts.py`:

```python
"""Unit tests for cashbook chart data prep (_expense_topn) + render."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from blueprints.cashbook import _expense_topn


def _cats(n):
    # n categories, descending totals (10, 9, 8, ...), like _get_category_summary
    return [{'category': f'c{i}', 'total': float(10 - i)} for i in range(n)]


def test_expense_topn_under_threshold_returned_asis():
    cats = [{'category': 'a', 'total': 5.0}, {'category': 'b', 'total': 3.0}]
    out = _expense_topn(cats, n=7)
    assert out == cats
    assert all(c['category'] != 'อื่นๆ' for c in out)


def test_expense_topn_exactly_threshold_no_other():
    cats = _cats(7)
    out = _expense_topn(cats, n=7)
    assert len(out) == 7
    assert all(c['category'] != 'อื่นๆ' for c in out)


def test_expense_topn_folds_tail_into_other():
    cats = _cats(9)
    out = _expense_topn(cats, n=7)
    assert len(out) == 8
    assert out[7]['category'] == 'อื่นๆ'
    assert out[7]['total'] == cats[7]['total'] + cats[8]['total']


def test_expense_topn_preserves_grand_total():
    cats = _cats(9)
    out = _expense_topn(cats, n=7)
    assert sum(c['total'] for c in out) == sum(c['total'] for c in cats)


def test_expense_topn_empty():
    assert _expense_topn([], n=7) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_charts.py -q`
Expected: FAIL — `ImportError: cannot import name '_expense_topn'`.

- [ ] **Step 3: Implement `_expense_topn` + pass `expense_chart`**

In `inventory_app/blueprints/cashbook.py`, add next to the other helpers (e.g. right after `_get_category_summary`):

```python
def _expense_topn(expense_cats, n=7):
    """Top-n expense categories by total (input already sorted desc); the rest
    folded into a single 'อื่นๆ' row. Grand total preserved. Pure/testable."""
    out = [{"category": c["category"], "total": c["total"]} for c in expense_cats[:n]]
    rest = expense_cats[n:]
    if rest:
        out.append({"category": "อื่นๆ", "total": sum(c["total"] for c in rest)})
    return out
```

In `dashboard()`'s `render_template(...)` call, add the kwarg (next to `expense_cats=expense_cats,`):

```python
        expense_cats=expense_cats,
        expense_chart=_expense_topn(expense_cats),
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_charts.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_cashbook_charts.py inventory_app/blueprints/cashbook.py
git commit -m "feat(cashbook): _expense_topn chart helper + expense_chart in dashboard context"
```

---

## Task 2: Charts markup + JS refactor + Chart.js init

**Files:**
- Modify: `inventory_app/templates/cashbook/dashboard.html`
- Test: `tests/test_cashbook_charts.py` (append render test)

- [ ] **Step 1: Add the charts row markup**

In `dashboard.html`, insert **after** the disclosure-note block (the `<div class="text-subtle small mb-4" role="note">…</div>` explaining คงเหลือ) and **before** the `{# ── Per-account table` comment:

```html
{# ── Charts ─────────────────────────────────────────────────────────────────── #}
<div class="row g-3 mb-4">
  <div class="col-lg-6">
    <div class="card h-100">
      <div class="card-header fw-semibold"><i class="bi bi-bar-chart me-1"></i>แนวโน้มรายเดือน</div>
      <div class="card-body">
        {% if monthly %}
        <div style="height:260px;"><canvas id="cbTrendChart"></canvas></div>
        {% else %}
        <div class="text-muted small">ยังไม่มีข้อมูลรายเดือน</div>
        {% endif %}
      </div>
    </div>
  </div>
  <div class="col-lg-6">
    <div class="card h-100">
      <div class="card-header fw-semibold"><i class="bi bi-pie-chart me-1"></i>สัดส่วนรายจ่ายตามหมวด</div>
      <div class="card-body">
        {% if expense_chart %}
        <div style="height:260px;"><canvas id="cbExpenseChart"></canvas></div>
        {% else %}
        <div class="text-muted small">ยังไม่มีข้อมูลรายจ่าย</div>
        {% endif %}
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Replace the `{% block scripts %}` block (refactor modal JS → `openCbDetail` + add charts)**

Replace the ENTIRE existing `{% block scripts %} … {% endblock %}` at the end of `dashboard.html` with:

```html
{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
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

  function openCbDetail(dim, key, label) {
    titleEl.textContent = label || key;
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
  }

  // Table rows
  document.querySelectorAll('[data-cb-dim]').forEach(row => {
    row.addEventListener('click', () => openCbDetail(
      row.getAttribute('data-cb-dim'),
      row.getAttribute('data-cb-key'),
      row.getAttribute('data-cb-label')));
  });

  // Charts
  const monthly = {{ monthly|tojson }};
  const expenseChart = {{ expense_chart|tojson }};

  const trendEl = document.getElementById('cbTrendChart');
  if (trendEl && monthly.length) {
    new Chart(trendEl, {
      data: {
        labels: monthly.map(m => m.month),
        datasets: [
          { type: 'bar', label: 'รายรับ', backgroundColor: '#2e7d3a', data: monthly.map(m => m.income) },
          { type: 'bar', label: 'รายจ่าย', backgroundColor: '#c41e2a', data: monthly.map(m => m.expense) },
          { type: 'line', label: 'คงเหลือสุทธิ', borderColor: '#0284c7', backgroundColor: '#0284c7',
            data: monthly.map(m => m.income - m.expense), tension: 0.3, fill: false }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' } },
        onClick: (e, els) => {
          if (!els.length) return;
          const m = monthly[els[0].index];
          openCbDetail('month', m.month, 'เดือน ' + m.month);
        }
      }
    });
  }

  const expEl = document.getElementById('cbExpenseChart');
  if (expEl && expenseChart.length) {
    const palette = ['#c41e2a','#e8590c','#f08c00','#e6a817','#2e7d3a','#0284c7','#6f42c1','#adb5bd'];
    new Chart(expEl, {
      type: 'doughnut',
      data: {
        labels: expenseChart.map(c => c.category),
        datasets: [{
          data: expenseChart.map(c => c.total),
          backgroundColor: expenseChart.map((c, i) => c.category === 'อื่นๆ' ? '#adb5bd' : palette[i % palette.length])
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'right' } },
        onClick: (e, els) => {
          if (!els.length) return;
          const cat = expenseChart[els[0].index].category;
          if (cat === 'อื่นๆ') return;
          openCbDetail('expense_category', cat, 'รายจ่าย: ' + cat);
        }
      }
    });
  }
})();
</script>
{% endblock %}
```

- [ ] **Step 3: Append the render test**

In `tests/test_cashbook_charts.py`, append:

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


def test_dashboard_has_charts(admin_client):
    html = admin_client.get('/cashbook/').data.decode('utf-8')
    assert 'id="cbTrendChart"' in html
    assert 'id="cbExpenseChart"' in html
    assert 'chart.umd.min.js' in html
    assert 'openCbDetail' in html   # refactor kept the modal opener
```

- [ ] **Step 4: Run the render test**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_charts.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Restart + smoke-check**

```bash
sendy-down && sendy-up && sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/cashbook/
```
Expected: `302` (anon redirect; not 500 — confirms no Jinja/`tojson`/`url_for` error at template parse). The render test (Step 4) exercises the authed render.

- [ ] **Step 6: Commit**

```bash
git add inventory_app/templates/cashbook/dashboard.html tests/test_cashbook_charts.py
git commit -m "feat(cashbook): monthly trend + expense donut charts, clickable into drill-down"
```

---

## Task 3: Full verification

- [ ] **Step 1: Cashbook suite**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_cashbook_charts.py tests/test_cashbook_detail.py tests/test_cashbook_pnl.py tests/test_bp_cashbook_routes.py -q`
Expected: all green.

- [ ] **Step 2: Full suite (no new breakage)**

Run: `cd sendy_erp && ~/.virtualenvs/erp/bin/pytest -q`
Expected: only the 3 cashbook-import + 2 HR-payroll pre-existing failures remain red.

- [ ] **Step 3: Manual browser check (Put)**

Restart sendy, open `http://127.0.0.1:5001/cashbook/`: confirm the trend bars + net line render, the donut shows Top 7 + อื่นๆ, and clicking a month bar / a category slice opens the drill-down modal with a matching total. Confirm table-row clicks still work (refactor intact).

---

## Self-review notes
- **Spec coverage:** Chart A (Task 2 init + trend markup), Chart B + `_expense_topn` (Task 1 + Task 2), clickable via `openCbDetail` refactor (Task 2), placement after disclosure note (Task 2 Step 1), Chart.js CDN in scripts block (Task 2 Step 2), tests for helper + render (Tasks 1, 2). Covered.
- **Type consistency:** `_expense_topn` returns `[{category, total}]`; template emits it as `expense_chart` via `|tojson`; JS reads `c.category` / `c.total`. `monthly` rows have `month/income/expense` (from `_get_monthly_summary`) — JS reads those. `openCbDetail(dim, key, label)` signature used identically by rows and both charts. Canvas ids `cbTrendChart` / `cbExpenseChart` match between markup, JS, and render test.
- **No placeholders:** all steps carry concrete code/commands.
