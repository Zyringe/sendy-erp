# Staff stock-adjust + tick-mark reasons + count-date lock — Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax. Spec: `docs/specs/2026-06-20-staff-stock-adjust-design.md`.

**Goal:** Let all logged-in roles use the **ปรับ** stock-adjust flow (a modal), pick a reason via tick-marks instead of typing, and stamp the date — locked to "now" for นับสต๊อก, backdatable otherwise.

**Architecture:** No schema change. `transactions.note` stores the reason label; `transactions.created_at` carries the (possibly back)date. `models.add_transaction` gains an optional `created_at`. The route maps a `reason` code → Thai label and computes `created_at`. Three forms (detail modal, alerts modal, fallback page) share one Jinja partial.

**Tech Stack:** Flask 3 / Python 3.9 / SQLite, Jinja2, Bootstrap 5 (already loaded app-wide), pytest.

**Working dir:** `~/Sendai-Boonsawat-wt/staff-stock-adjust/` (isolated worktree off origin/main). Run tests from `~/Sendai-Boonsawat-wt/staff-stock-adjust/` with `~/.virtualenvs/erp/bin/pytest`. **Do NOT `git commit`** — leave changes in the working tree; the orchestrator commits after review.

## Global Constraints (verbatim, apply to every task)

- **Reason → `note` label map** (exact strings):
  `count`→`นับสต๊อก`, `damaged`→`ชำรุด / แตกหัก`, `lost`→`สูญหาย`, `sample`→`ของแถม / เบิกใช้เอง`, `correction`→`แก้ยอดผิด`, `other`→ staff-typed `note_other` (verbatim, stripped).
- **Form field contract** (what the templates submit, what the route reads):
  `new_quantity` (int ≥ 0, existing), `reason` (one of the 6 codes), `note_other` (text, required iff `reason==other`), `adjust_date` (`YYYY-MM-DD`; required for non-count, ignored for count), `next` (hidden internal path), `csrf_token`.
- **Date rule:** `reason==count` ⇒ `created_at=None` (DB default `datetime('now','localtime')`). Else parse `adjust_date`; reject if malformed or `> today`; `created_at = None if adjust_date==today else f'{adjust_date} 00:00:00'`.
- **Permissions:** add `'stock_adjust'` to `_STAFF_POST_OK` only (manager inherits). รับเข้า/จ่ายออก stay admin-only.
- **No migration. No new dependency. CSRF token required in every POST form.** Python 3.9 (no `int | None`).
- **Scope:** only the ปรับ flow. Do not touch stock_in / stock_out.

---

### Task 1: `add_transaction` accepts an optional `created_at`

**Files:**
- Modify: `inventory_app/models.py:422-430`
- Test: `tests/test_stock_adjust.py` (new file; this task adds the first test)

**Interfaces:**
- Produces: `models.add_transaction(product_id, txn_type, quantity_change, unit_mode, reference_no=None, note=None, created_at=None)` — when `created_at is None`, column is omitted (DB default fires); otherwise inserted verbatim.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stock_adjust.py`:
```python
"""Stock-adjust route + add_transaction created_at tests."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')
import sqlite3
from datetime import date, timedelta
import pytest


def _first_active_product_id(db) -> int:
    row = sqlite3.connect(db).execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        pytest.skip("No active products in live DB clone")
    return row[0]


def _latest_txn(db, pid):
    return sqlite3.connect(db).execute(
        "SELECT txn_type, quantity_change, note, created_at FROM transactions "
        "WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()


def test_add_transaction_custom_created_at(tmp_db):
    import models
    pid = _first_active_product_id(tmp_db)
    models.add_transaction(pid, 'ADJUST', 1, 'unit', note='นับสต๊อก',
                           created_at='2025-01-15 00:00:00')
    row = _latest_txn(tmp_db, pid)
    assert row[0] == 'ADJUST'
    assert row[3] == '2025-01-15 00:00:00'


def test_add_transaction_default_created_at_is_now(tmp_db):
    import models
    pid = _first_active_product_id(tmp_db)
    models.add_transaction(pid, 'ADJUST', 1, 'unit', note='นับสต๊อก')
    row = _latest_txn(tmp_db, pid)
    assert row[3].startswith(date.today().isoformat())
```

- [ ] **Step 2: Run, verify the custom-date test fails**

Run: `~/.virtualenvs/erp/bin/pytest tests/test_stock_adjust.py::test_add_transaction_custom_created_at -v`
Expected: FAIL — `add_transaction() got an unexpected keyword argument 'created_at'`.

- [ ] **Step 3: Implement**

Replace `inventory_app/models.py:422-430` with:
```python
def add_transaction(product_id: int, txn_type: str, quantity_change: int,
                    unit_mode: str, reference_no=None, note=None, created_at=None):
    conn = get_connection()
    if created_at is None:
        conn.execute("""
            INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (product_id, txn_type, quantity_change, unit_mode, reference_no, note))
    else:
        conn.execute("""
            INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at))
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Run both Task-1 tests, verify pass**

Run: `~/.virtualenvs/erp/bin/pytest tests/test_stock_adjust.py -v`
Expected: 2 passed.

- [ ] **Step 5: Regression — existing callers unaffected**

Run: `~/.virtualenvs/erp/bin/pytest -q -k "stock or transaction or import"`
Expected: no new failures (the new param defaults to None; IN/OUT callers untouched).

---

### Task 2: `stock_adjust` route — permissions + reason + date logic

**Files:**
- Modify: `inventory_app/app.py:191` (`_STAFF_POST_OK`)
- Modify: `inventory_app/app.py:1223-1265` (`stock_adjust`)
- Test: `tests/test_stock_adjust.py` (append)

**Interfaces:**
- Consumes: `models.add_transaction(..., note=, created_at=)` (Task 1); `models.get_current_stock(pid)`.
- Produces: POST `/products/<id>/adjust` accepting the field contract; GET still renders `transactions/adjust_form.html` with `today=date.today().isoformat()`.

- [ ] **Step 1: Write failing route tests** (append to `tests/test_stock_adjust.py`)

```python
@pytest.fixture
def staff_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 2; sess['username'] = 'test-staff'; sess['role'] = 'staff'
    return c


@pytest.fixture
def manager_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 3; sess['username'] = 'test-mgr'; sess['role'] = 'manager'
    return c


def _current(db, pid):
    r = sqlite3.connect(db).execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return r[0] if r else 0


def _txn_count(db, pid):
    return sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]


def test_staff_can_adjust_count(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 2
    resp = staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'count', 'adjust_date': '2020-01-01'})
    assert resp.status_code in (302, 303)
    row = _latest_txn(tmp_db, pid)
    assert row[0] == 'ADJUST' and row[2] == 'นับสต๊อก'
    # count ignores the submitted backdate → stamped today/now
    assert row[3].startswith(date.today().isoformat())


def test_manager_can_adjust(manager_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 3
    resp = manager_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'correction', 'adjust_date': date.today().isoformat()})
    assert resp.status_code in (302, 303)
    assert _latest_txn(tmp_db, pid)[2] == 'แก้ยอดผิด'


def test_backdate_non_count_lands_on_date(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) - 1
    past = (date.today() - timedelta(days=10)).isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'damaged', 'adjust_date': past})
    row = _latest_txn(tmp_db, pid)
    assert row[2] == 'ชำรุด / แตกหัก'
    assert row[3] == f'{past} 00:00:00'


def test_today_non_count_stamps_now(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 5
    today = date.today().isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'lost', 'adjust_date': today})
    ca = _latest_txn(tmp_db, pid)[3]
    assert ca.startswith(today) and ca != f'{today} 00:00:00'  # has a real time


def test_other_requires_text(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'other', 'note_other': '   ',
              'adjust_date': date.today().isoformat()})
    assert _txn_count(tmp_db, pid) == before  # rejected, no row


def test_other_with_text_stores_verbatim(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'other', 'note_other': 'ยกชุดไปงาน',
              'adjust_date': date.today().isoformat()})
    assert _latest_txn(tmp_db, pid)[2] == 'ยกชุดไปงาน'


def test_future_date_rejected(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    future = (date.today() + timedelta(days=3)).isoformat()
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'damaged', 'adjust_date': future})
    assert _txn_count(tmp_db, pid) == before


def test_invalid_reason_rejected(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    new_q = _current(tmp_db, pid) + 1
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': new_q, 'reason': 'bogus', 'adjust_date': date.today().isoformat()})
    assert _txn_count(tmp_db, pid) == before


def test_zero_diff_no_row(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    before = _txn_count(tmp_db, pid)
    same = _current(tmp_db, pid)
    staff_client.post(f'/products/{pid}/adjust',
        data={'new_quantity': same, 'reason': 'count'})
    assert _txn_count(tmp_db, pid) == before
```

- [ ] **Step 2: Run, verify failures**

Run: `~/.virtualenvs/erp/bin/pytest tests/test_stock_adjust.py -v`
Expected: the new route tests FAIL — staff POST currently 302-redirects to dashboard (not in whitelist) and the route doesn't read `reason`.

- [ ] **Step 3a: Add `stock_adjust` to the staff whitelist**

In `inventory_app/app.py:191` `_STAFF_POST_OK = frozenset([...])`, add the line:
```python
    'stock_adjust',
```
(near the other operation endpoints; manager inherits via `_MANAGER_POST_OK = _STAFF_POST_OK | {...}`).

- [ ] **Step 3b: Ensure `date` is imported**

Confirm `inventory_app/app.py` imports `date` and `datetime` from `datetime`. If only `datetime` is imported, change the import to `from datetime import datetime, date`. (Grep first: `grep -n "from datetime import" inventory_app/app.py`.)

- [ ] **Step 3c: Rewrite the POST body of `stock_adjust`**

Replace the POST branch in `stock_adjust` (`inventory_app/app.py`, currently ~1239-1263, the `if request.method == 'POST':` block) with:
```python
    REASON_LABELS = {
        'count': 'นับสต๊อก',
        'damaged': 'ชำรุด / แตกหัก',
        'lost': 'สูญหาย',
        'sample': 'ของแถม / เบิกใช้เอง',
        'correction': 'แก้ยอดผิด',
    }

    if request.method == 'POST':
        f = request.form
        # quantity
        try:
            new_qty = int(f['new_quantity'])
            if new_qty < 0:
                raise ValueError('จำนวนต้องไม่ติดลบ')
        except (KeyError, ValueError) as e:
            flash(str(e) or 'จำนวนไม่ถูกต้อง', 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        # reason -> note
        reason = f.get('reason', '')
        if reason == 'other':
            note = f.get('note_other', '').strip()
            if not note:
                flash('กรุณาระบุเหตุผล', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
        elif reason in REASON_LABELS:
            note = REASON_LABELS[reason]
        else:
            flash('กรุณาเลือกเหตุผลในการปรับยอด', 'danger')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        # date / created_at
        today = date.today().isoformat()
        if reason == 'count':
            created_at = None  # DB default = datetime('now','localtime')
        else:
            adj = f.get('adjust_date', '').strip()
            try:
                d = datetime.strptime(adj, '%Y-%m-%d').date()
            except ValueError:
                flash('วันที่ไม่ถูกต้อง', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
            if d > date.today():
                flash('วันที่ปรับต้องไม่เกินวันนี้', 'danger')
                return redirect(_safe_next('products.product_detail', product_id=product_id))
            created_at = None if adj == today else f'{adj} 00:00:00'

        # diff
        current = models.get_current_stock(product_id)
        diff = new_qty - current
        if diff == 0:
            flash('จำนวนเท่าเดิม ไม่มีการเปลี่ยนแปลง', 'info')
            return redirect(_safe_next('products.product_detail', product_id=product_id))

        models.add_transaction(product_id, 'ADJUST', diff, 'unit', note=note, created_at=created_at)
        flash(f'ปรับยอดสต็อกเป็น {new_qty} {product["unit_type"]} เรียบร้อย', 'success')
        return redirect(_safe_next('products.product_detail', product_id=product_id))

    return render_template('transactions/adjust_form.html', product=product,
                           today=date.today().isoformat())
```
(`REASON_LABELS` may instead be a module-level constant near the top of the route group — keep it readable; do not duplicate it.)

- [ ] **Step 4: Run the full file, verify pass**

Run: `~/.virtualenvs/erp/bin/pytest tests/test_stock_adjust.py -v`
Expected: all tests pass (12).

- [ ] **Step 5: Regression sweep**

Run: `~/.virtualenvs/erp/bin/pytest -q`
Expected: no NEW failures vs origin/main baseline. (Pre-existing skips/failures unrelated to this change are acceptable — note them.)

---

### Task 3: Shared partial + detail modal + alerts modal + fallback page

**Files:**
- Create: `inventory_app/templates/transactions/_adjust_fields.html`
- Modify: `inventory_app/templates/transactions/adjust_form.html`
- Modify: `inventory_app/templates/products/detail.html:85-107`
- Modify: `inventory_app/templates/alerts.html:39, 68-112`

**Interfaces:**
- Consumes: the field contract from Global Constraints. The route (Task 2) reads `reason`, `note_other`, `adjust_date`, `new_quantity`, `next`.
- Produces: nothing for later tasks. Verified by rendering, not unit tests.

- [ ] **Step 1: Create the shared partial**

`inventory_app/templates/transactions/_adjust_fields.html`:
```html
{# Shared reason-picker + date field for stock-adjust (modals + fallback page).
   Included ONCE per page. Today's date/max set client-side. #}
<div class="mb-3">
  <label class="form-label small mb-1 fw-600">เหตุผลที่ปรับ <span class="text-danger">*</span></label>
  <div class="d-grid gap-2" id="adj-reasons">
    {% set _reasons = [
      ('count','นับสต๊อก (เช็คของจริง)','ล็อกวันที่เป็นวันนี้'),
      ('damaged','ชำรุด / แตกหัก',''),
      ('lost','สูญหาย',''),
      ('sample','ของแถม / เบิกใช้เอง',''),
      ('correction','แก้ยอดผิด',''),
      ('other','อื่นๆ','พิมพ์เพิ่ม') ] %}
    {% for val, label, hint in _reasons %}
    <label class="adj-reason d-flex align-items-center gap-2 border rounded-3 px-3 py-2 mb-0" style="cursor:pointer">
      <input class="form-check-input mt-0" type="radio" name="reason" value="{{ val }}" {% if val == 'count' %}checked{% endif %}>
      <span>{{ label }}</span>
      {% if hint %}<span class="ms-auto text-subtle small">{{ hint }}</span>{% endif %}
    </label>
    {% endfor %}
  </div>
  <div class="mt-2 d-none" id="adj-other-box">
    <input type="text" class="form-control" name="note_other" maxlength="200" placeholder="ระบุเหตุผล...">
  </div>
</div>
<div class="mb-2">
  <label class="form-label small mb-1 fw-600">วันที่ปรับ <span class="text-danger">*</span></label>
  <input type="date" class="form-control" name="adjust_date" id="adj-date">
  <div class="form-text small" id="adj-date-hint"></div>
</div>
<script>
(function(){
  var today = new Date().toLocaleDateString('en-CA');
  var d = document.getElementById('adj-date');
  var hint = document.getElementById('adj-date-hint');
  var otherBox = document.getElementById('adj-other-box');
  var group = document.getElementById('adj-reasons');
  if (!d || !group) return;
  d.max = today; if (!d.value) d.value = today;
  function sel(){ var r = group.querySelector('input[name=reason]:checked'); return r ? r.value : ''; }
  function refresh(){
    var r = sel();
    otherBox.classList.toggle('d-none', r !== 'other');
    if (r === 'count'){ d.value = today; d.disabled = true;
      hint.innerHTML = '🔒 ล็อกเป็นวันนี้ — บันทึกเป็นเวลาจริงที่กดยืนยัน'; }
    else { d.disabled = false; hint.textContent = 'เลือกย้อนหลังได้ — ห้ามเกินวันนี้'; }
    group.querySelectorAll('.adj-reason').forEach(function(el){
      var on = el.querySelector('input').checked;
      el.classList.toggle('border-warning', on);
      el.classList.toggle('bg-warning-subtle', on);
    });
  }
  group.addEventListener('change', refresh);
  refresh();
})();
</script>
```

- [ ] **Step 2: Fallback page uses the partial**

In `inventory_app/templates/transactions/adjust_form.html`, replace the free-text `หมายเหตุ` textarea block (the `<div class="mb-4">…<textarea name="note">…</textarea></div>`) with:
```html
  {% include 'transactions/_adjust_fields.html' %}
```
Leave the `new_quantity` input and the submit button as-is. (The `note` textarea is fully removed — `note` is no longer submitted by this form.)

- [ ] **Step 3: Detail page — ปรับ becomes a modal trigger (all roles) + add modal**

In `inventory_app/templates/products/detail.html`, restructure the footer (currently `{% if is_admin %}` wraps all 3 buttons, lines ~85-107) so รับเข้า/จ่ายออก stay admin-only but ปรับ shows to everyone and opens the modal:
```html
      <div class="card-footer d-flex gap-2">
        {% if is_admin %}
        <a href="{{ url_for('stock_in', product_id=product.id) }}"
           class="btn btn-sm flex-fill fw-500" style="background:#dcf2e0;color:#2e7d3a;border:none">
          <i class="bi bi-arrow-down-circle me-1"></i>รับเข้า</a>
        <a href="{{ url_for('stock_out', product_id=product.id) }}"
           class="btn btn-sm flex-fill fw-500" style="background:#fdecee;color:#c41e2a;border:none">
          <i class="bi bi-arrow-up-circle me-1"></i>จ่ายออก</a>
        {% endif %}
        <button type="button" class="btn btn-sm flex-fill fw-500"
                data-bs-toggle="modal" data-bs-target="#adjustModal"
                style="background:#f1f5f9;color:#475569;border:none">
          <i class="bi bi-sliders me-1"></i>ปรับ</button>
      </div>
```
Keep the existing `{# Online stock … #}` comment + the `{% endif %}` that closes the card body block — verify the conditional nesting stays balanced after the edit (the old `{% if is_admin %}` that opened at line 85 is now moved INSIDE the footer; the outer block end at line 107 must still match its real opener — read the surrounding template and keep all `{% if %}/{% endif %}` balanced).

Then add the modal. Place it just before the page's `{% endblock %}` (or alongside other page-level markup). Use the product's current-stock variable **that this template already uses to display stock** (grep the template for how it shows the on-hand quantity — e.g. `stock`, `current_stock`, or `product.quantity` — and reuse that exact expression; do not invent one):
```html
{# ── Adjust modal (all logged-in roles) ── #}
<div class="modal fade" id="adjustModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <form method="post" action="{{ url_for('stock_adjust', product_id=product.id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="next" value="{{ url_for('products.product_detail', product_id=product.id) }}">
        <div class="modal-header">
          <h5 class="modal-title"><i class="bi bi-sliders me-2"></i>ปรับยอดสต็อก</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="ปิด"></button>
        </div>
        <div class="modal-body">
          <div class="mb-3 small text-subtle">สต็อกปัจจุบัน:
            <strong class="text-main">{{ CURRENT_STOCK_EXPR }}</strong> {{ product.unit_type }}</div>
          <div class="mb-3">
            <label class="form-label small mb-1 fw-600">ยอดสต็อกที่ถูกต้อง <span class="text-danger">*</span></label>
            <div class="input-group">
              <input type="number" name="new_quantity" class="form-control form-control-lg" min="0" required placeholder="0">
              <span class="input-group-text">{{ product.unit_type }}</span>
            </div>
          </div>
          {% include 'transactions/_adjust_fields.html' %}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">ยกเลิก</button>
          <button type="submit" class="btn fw-500" style="background:#f5a800;border-color:#f5a800;color:#fff">
            <i class="bi bi-check-circle me-1"></i>ยืนยันการปรับยอด</button>
        </div>
      </form>
    </div>
  </div>
</div>
```
Replace `CURRENT_STOCK_EXPR` with the real on-hand expression found in the template.

- [ ] **Step 4: Alerts page — ungate + use the partial**

In `inventory_app/templates/alerts.html`:
1. Remove the `{% if is_admin %}` / `{% endif %}` that gate the per-row **ปรับสต็อก** button (lines ~39, 49) so all roles see it.
2. Remove the `{% if is_admin %}` / `{% endif %}` that gate the modal + its script (lines ~69, and the matching `{% endif %}` after the `<script>`), so the modal renders for all roles.
3. Inside the modal `<form>`, replace the free-text note block (the `<div class="mb-2">…<input … name="note" … required></div>`, lines ~97-101) with:
   ```html
   {% include 'transactions/_adjust_fields.html' %}
   ```
   Keep the existing `new_quantity` input (`#adj-new-qty`), the hidden `next`, csrf, and the `.adjust-btn` wiring `<script>` (it sets the form `action` + fills SKU/name/current per row) — do NOT remove that script. The partial's own script is separate and coexists.

- [ ] **Step 5: Verify templates render via pytest (safe `tmp_db` clone, never the live DB)**

Append to `tests/test_stock_adjust.py` (reuses the `staff_client` + `tmp_db` fixtures and helpers from Task 2; these assert the partial/modal compile and that staff can see the alerts page):
```python
def test_detail_page_has_adjust_modal(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    body = staff_client.get(f'/products/{pid}').get_data(as_text=True)
    assert 'id="adjustModal"' in body
    assert 'นับสต๊อก' in body
    assert 'name="reason"' in body
    assert 'name="adjust_date"' in body


def test_adjust_fallback_page_renders(staff_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    r = staff_client.get(f'/products/{pid}/adjust')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="reason"' in body
    assert '<textarea name="note"' not in body  # old free-text note removed


def test_alerts_page_renders_for_staff(staff_client, tmp_db):
    r = staff_client.get('/alerts')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="reason"' in body  # modal + partial now visible to staff
```

Run: `~/.virtualenvs/erp/bin/pytest tests/test_stock_adjust.py -v`
Expected: all pass — including the 3 render tests. A Jinja error in the partial/modal/alerts surfaces here as a 500 in the GET. (The `tmp_db` fixture clones the live DB read-only into a temp file; it never writes the real DB.)

---

### Task 4: Integration verification (orchestrator / pre-merge gate)

> Not an agent task — the orchestrator runs this against a real booted Sendy before any merge (merge auto-deploys to prod). Listed here so it isn't skipped.

- [ ] Full suite green: `~/.virtualenvs/erp/bin/pytest -q` (no new failures vs baseline).
- [ ] Boot a real Sendy on the branch code; `curl` `GET /products/<id>/adjust` and `/alerts` → 200; restart after template/route edits.
- [ ] As a **staff** session in a browser: open a product → ปรับ → submit นับสต๊อก (date locked) and a backdated ชำรุด → confirm two `transactions` rows with correct `note` + `created_at` (`now` vs `<date> 00:00:00`).
- [ ] Put does the 30-sec click-through (radio toggle, date lock, อื่นๆ box). Only then commit + PR + merge.

## Self-review notes

- Spec coverage: permissions (T2 s3a), tick-mark reasons (T3 partial), `other` text (T2 + T3), date field + count-lock (T2 date rule + T3 JS), modal entry (T3 s3), `00:00:00`-only-for-backdate (T2 + tests), no migration (none added), shared partial (T3 s1), alerts modal (T3 s4), fallback page (T3 s2). All covered.
- Type consistency: `add_transaction(..., created_at=)` defined T1, consumed T2; `reason` codes + `REASON_LABELS` identical across T2/T3; field names match the contract.
- The render-check DB caveat is called out (worktrees lack `instance/inventory.db`).
