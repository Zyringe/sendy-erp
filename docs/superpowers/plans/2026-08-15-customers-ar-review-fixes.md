# Customers & AR Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Sendy's Customers & AR workflows fail loud on stale/incomplete source data, synchronize receipt allocation exactly, report invoice status without multiplication/cancelled-receipt errors, and preserve an attributable collection history.

**Architecture:** Keep the Express AR snapshot as the authoritative collection balance and the Sendy sales/payment ledger as a diagnostic. Add an AR-specific freshness contract, make each imported receipt replace its complete child-link set, and make every legacy payment-status reader use active receipts at one-invoice grain. Reuse the existing call-log soft-delete pattern for AR outreach and harden customer/follow-up request boundaries without adding new frameworks or dependencies.

**Tech Stack:** Python 3.9, Flask 3.x, SQLite, Jinja, pytest.

**Spec:** `/Users/putty/Sendai-Boonsawat/Operations/05_analysis-reports/engineering/customers_ar_review_2026-08-15.md`

## Global Constraints

- Read `/Users/putty/Sendai-Boonsawat/CLAUDE.md`, `/Users/putty/Sendai-Boonsawat/references/workspace-operating-manual.md`, `/Users/putty/Sendai-Boonsawat/sendy_erp/CLAUDE.md`, `/Users/putty/Sendai-Boonsawat/.claude/rules/erp-engineering-discipline.md`, and `/Users/putty/Sendai-Boonsawat/.claude/rules/verification-discipline.md` before editing.
- Fetch `origin` first. Create an isolated worktree from the freshly fetched `origin/main`; suggested branch `fix/customers-ar-review`. Never edit the shared checkout, which contains unrelated Hammer Pack and review-plan work.
- Before any test or app boot, prove the worktree has no symlink/shared path to `/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db`. Tests and HTTP checks must use a disposable `sqlite3 .backup` clone.
- TDD is mandatory for every finding: capture RED on unmodified fresh `origin/main`, verify the failure is the intended contract failure, then make the smallest implementation GREEN.
- No new dependency, service, AR engine, background job, cache, or speculative abstraction.
- `express_ar_outstanding` remains authoritative for collections. `sales_transactions` + `received_payments` + `paid_invoices` remain diagnostic/reconciliation data.
- Put's customer-code decision (2026-08-15): every customer code currently present in Express is legitimate input and must be imported, including shapes such as `L1004ค01`, `1101ค01`, and `23ทธ01`. Do not enforce the former `two digits + one letter + digits` grammar.
- Put authorizes read-only access to the current Express DBF source. Resolve disputed receipt allocations from `ARRCPIT.DBF`; never choose a link from Sendy inference or hand-edit the business DB.
- AR snapshot staleness rule for this implementation: `age_days > 1` is stale. This matches the existing approximately-daily Express freshness expectation without pretending the DBF transaction import refreshed AR.
- Migration 159 belongs to the `db-architect` specialist. After fetching, verify 158 is still the highest migration. If migration 159 already exists, stop and report the collision; do not silently renumber or edit another migration.
- Any schema change requires `159_*.sql`, matching `159_*.rollback.sql`, regeneration with `~/.virtualenvs/erp/bin/python scripts/dump_schema.py`, migration tests, rollback tests, and fresh-schema verification.
- Preserve audit triggers when synchronizing `paid_invoices`; deleting stale links must flow through the existing delete trigger.
- Run an independent read-only `code-reviewer` pass before each task commit. Resolve every BLOCKER/IMPORTANT finding and rerun that task's RED/GREEN and focused suite.
- Do not modify `/Users/putty/Sendai-Boonsawat/projects/codex-review/README.md` or `/Users/putty/Sendai-Boonsawat/projects/README.md` from the Sendy branch.
- Stop before merge, push-to-production, deployment, production mutation, or tracker promotion. Customers & AR stays 🟠 until Codex reviews the diff and later verifies production.

---

### Task 1: Give the Authoritative AR Snapshot Its Own Freshness Contract

**Files:**
- Modify: `inventory_app/cashflow.py:165-276`
- Modify: `inventory_app/blueprints/accounting.py:254-358,597-632`
- Modify: `inventory_app/templates/ar.html:27-30`
- Modify: `inventory_app/templates/ar_followup_detail.html:5-12`
- Modify: `inventory_app/models/imports.py:482-507`
- Modify: `inventory_app/templates/dashboard.html:24-37`
- Test: `tests/test_cashflow.py`
- Test: `tests/test_ar_page.py`
- Test: `tests/test_ar_followup.py`
- Test: `tests/test_express_dbf_upload_route.py`

**Interfaces:**
- Produces: `cashflow.ar_aging()` keys `age_days: int` and `is_stale: bool` in both populated and empty results.
- Produces: AR templates receive one `aging` object and render an AR-specific danger banner when `aging.is_stale`.
- Preserves: `models.get_express_dbf_freshness()` as transactional-DBF freshness, but UI copy must say that explicitly and never imply AR was refreshed.

- [ ] **Step 1: Write failing freshness tests**

Add deterministic `as_of` coverage in `tests/test_cashflow.py` using an Express snapshot dated `2026-06-05`:

```python
def test_ar_aging_reports_snapshot_staleness(empty_db_conn):
    _ins_express_snap(empty_db_conn, '2026-06-05', 'C-ST', 'ร้านเก่า',
                      'IV-ST', '2026-06-01', 1000, 0, 1000)
    empty_db_conn.commit()

    result = cashflow.ar_aging(as_of='2026-08-15', conn=empty_db_conn)

    assert result['as_of'] == '2026-06-05'
    assert result['age_days'] == 71
    assert result['is_stale'] is True
```

Add a fresh control with snapshot `2026-08-14`, `as_of='2026-08-15'`, expecting `age_days == 1` and `is_stale is False`. Add an empty-snapshot control expecting `is_stale is True` and `age_days is None`.

In `tests/test_ar_page.py`, seed/force an old latest snapshot and assert every `/ar` tab contains `ข้อมูลลูกหนี้เก่าเกิน 1 วัน` and the exact snapshot date. In `tests/test_ar_followup.py`, assert the detail page receives/renders the same warning. In `tests/test_express_dbf_upload_route.py`, assert dashboard copy contains `ข้อมูลธุรกรรมจาก Express` and does not claim `ยอด AR` was updated.

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_cashflow.py \
  tests/test_ar_page.py \
  tests/test_ar_followup.py \
  tests/test_express_dbf_upload_route.py -q
```

Expected: new tests fail because `age_days`/`is_stale` and AR-page warnings do not exist; existing tests remain green.

- [ ] **Step 3: Implement the minimum freshness calculation**

Inside `ar_aging()`, use the supplied `as_of` only as the observation date; keep the snapshot date as the aging reference for invoice buckets:

```python
observation_date = date.fromisoformat(as_of or _today_iso())
snapshot_age_days = None if not snap else max((observation_date - date.fromisoformat(snap)).days, 0)
```

Return:

```python
'age_days': snapshot_age_days,
'is_stale': snapshot_age_days is None or snapshot_age_days > 1,
```

Compute `aging = cf_mod.ar_aging()` once at the top of `ar_dashboard()` and pass that same object into the overview instead of querying twice. Pass `aging` to `ar_followup_customer()`. Render a danger alert at the top of both AR templates when stale; include snapshot date and a link to the correct AR snapshot import route (`bsn.unified_import`). Change the general dashboard label to “ข้อมูลธุรกรรมจาก Express” so it cannot masquerade as AR freshness.

- [ ] **Step 4: Run GREEN and mutation control**

Run the Task 1 command. Then temporarily change `> 1` to `> 71`; verify the stale test fails for the threshold reason; restore and rerun green.

- [ ] **Step 5: Independent review and commit**

Review for duplicate queries, date-vs-datetime confusion, and whether every AR tab/detail gets the warning. Commit:

```bash
git add inventory_app/cashflow.py inventory_app/blueprints/accounting.py \
  inventory_app/templates/ar.html inventory_app/templates/ar_followup_detail.html \
  inventory_app/models/imports.py inventory_app/templates/dashboard.html \
  tests/test_cashflow.py tests/test_ar_page.py tests/test_ar_followup.py \
  tests/test_express_dbf_upload_route.py
git commit -m "ar: warn when authoritative snapshot is stale"
```

---

### Task 2: Make Receipt Imports Replace Their Complete Allocation Set

**Files:**
- Modify: `inventory_app/models/payments.py:107-220`
- Test: `tests/test_payment_parse.py`
- Test: `tests/test_import_payments_b2a_regression.py`

**Interfaces:**
- Consumes: each parsed payment record's complete `iv_list`.
- Produces: after each successful record savepoint, database links for that `re_id` exactly equal `{(iv_no, kind, amount) for iv in record['iv_list']}`.

- [ ] **Step 1: Write the removed-link regression**

Add a direct-record test that imports v1 with invoices A+B, then v2 with only A:

```python
def test_reimport_removes_invoice_links_absent_from_authoritative_receipt(tmp_db_conn):
    import models
    base = {
        're_no': 'RE-REPLACE-1', 'date_iso': '2026-08-01',
        'customer': 'ลูกค้าทดสอบ', 'salesperson': '00',
        'cancelled': False,
    }
    models.import_payment_records([{**base, 'total': 300.0, 'iv_list': [
        {'iv_no': 'IV-KEEP', 'kind': 'IV', 'amount': 100.0},
        {'iv_no': 'IV-DROP', 'kind': 'IV', 'amount': 200.0},
    ]}])
    models.import_payment_records([{**base, 'total': 100.0, 'iv_list': [
        {'iv_no': 'IV-KEEP', 'kind': 'IV', 'amount': 100.0},
    ]}])

    rows = tmp_db_conn.execute(
        """SELECT pi.doc_no, pi.amount
             FROM paid_invoices pi JOIN received_payments rp ON rp.id=pi.re_id
            WHERE rp.re_no=? ORDER BY pi.doc_no""",
        ('RE-REPLACE-1',),
    ).fetchall()
    assert [tuple(r) for r in rows] == [('IV-KEEP', 100.0)]
```

Add controls proving: identical replay is unchanged; an empty authoritative `iv_list` removes every old child; a record that fails after synchronization rolls back header and children to their pre-record state; links belonging to another `re_id` are untouched.

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_payment_parse.py \
  tests/test_import_payments_b2a_regression.py -q
```

Expected: removed-link and empty-list tests fail because stale children remain.

- [ ] **Step 3: Synchronize within the existing savepoint**

Immediately after resolving `re_id` and before child upserts:

```python
incoming_doc_nos = [iv['iv_no'] for iv in r['iv_list']]
if incoming_doc_nos:
    placeholders = ','.join('?' for _ in incoming_doc_nos)
    conn.execute(
        f"DELETE FROM paid_invoices WHERE re_id=? AND doc_no NOT IN ({placeholders})",
        (re_id, *incoming_doc_nos),
    )
else:
    conn.execute("DELETE FROM paid_invoices WHERE re_id=?", (re_id,))
```

Keep the existing upserts and savepoint. Do not delete by `doc_no` globally. Do not bypass the existing audit trigger.

- [ ] **Step 4: Run GREEN and break-it-once**

Run the Task 2 command. Then temporarily change the delete predicate from `re_id=?` to an impossible id; verify the removed-link test goes red; restore and rerun.

- [ ] **Step 5: Validate current-data transition on a disposable clone**

Read the current Express `ARRCPIT.DBF` source directly and record the rows whose `RCPNUM` is `RE6900328`, `RE6900331`, or `RE6900336`, including `DOCNUM`, `RECTYP`, and `RCVAMT`. That source—not Sendy's current links—decides which receipt owns `IV6900907` and `IV6900976`.

On a `.backup` clone only, import the current authoritative records through the real importer and show whether those two invoices lose stale duplicate links. Record source rows, before/after Sendy links, receipt-header totals, and settlement results. Do not hand-edit either clone or business DB to manufacture the expected answer.

- [ ] **Step 6: Independent review and commit**

Review savepoint rollback, empty input, audit-trigger behavior, and scope-by-`re_id`. Commit:

```bash
git add inventory_app/models/payments.py tests/test_payment_parse.py \
  tests/test_import_payments_b2a_regression.py
git commit -m "payments: replace stale receipt allocations on import"
```

---

### Task 3: Compute Legacy Invoice Status at One-Invoice Grain Using Active Receipts

**Files:**
- Modify: `inventory_app/models/payments.py:223-460`
- Test: `tests/test_ar_page.py`
- Test: `tests/test_payments_alloc.py`

**Interfaces:**
- Produces: one reusable SQL predicate/CTE for “invoice has at least one non-cancelled receipt.”
- Guarantees: `paid_count + unpaid_count == total_bills`, `paid_count <= total_bills`, and multiple active receipt links never multiply bill amount.

- [ ] **Step 1: Add four failing contracts**

Add tests that seed one invoice and two active receipt links, then assert summary counts the invoice and baht once. Add a cancelled-only link and assert the invoice is unpaid in `get_payment_status()`, `get_payment_summary()`, `get_ar_reconciliation()`, and `find_payment_candidates()`. Add a mixed cancelled+active control that remains paid.

The summary invariant must be explicit:

```python
summary = dict(models.get_payment_summary())
assert summary['paid_count'] + summary['unpaid_count'] == summary['total_bills']
assert summary['paid_count'] <= summary['total_bills']
assert summary['paid_amount'] == pytest.approx(expected_unique_paid_baht)
```

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_ar_page.py tests/test_payments_alloc.py -q
```

Expected: multi-link count/amount and cancelled-only status assertions fail.

- [ ] **Step 3: Replace link-grain joins with an active-doc CTE**

Use this shape in the invoice list, summary, reconciliation ledger, and candidate query:

```sql
WITH active_paid_docs AS (
    SELECT DISTINCT pi.doc_no
      FROM paid_invoices pi
      JOIN received_payments rp ON rp.id = pi.re_id
     WHERE rp.cancelled = 0
)
```

Join `active_paid_docs apd ON apd.doc_no = st.doc_base`. Compute paid from `apd.doc_no IS NOT NULL` and unpaid from `apd.doc_no IS NULL`. Do not join raw `paid_invoices` into bill aggregation. For display-only latest payment date/receipt, use a separate one-row-per-doc grouped CTE over active receipts.

- [ ] **Step 4: Run GREEN and four-way mutation matrix**

Run Task 3 tests. Then independently mutate/remove: `DISTINCT`, `rp.cancelled=0`, the unpaid null check, and the grouped latest-payment CTE. Each corresponding control must go red for its own reason. Restore and rerun green.

- [ ] **Step 5: Recalculate current-data evidence read-only**

On the disposable clone, confirm summary no longer reports more paid bills than total and record the new count/baht. Do not treat this diagnostic total as the authoritative Express AR total.

- [ ] **Step 6: Independent review and commit**

Review every reader named in Finding 4, query grain, pagination count parity, and query plans on current-sized data. Commit:

```bash
git add inventory_app/models/payments.py tests/test_ar_page.py tests/test_payments_alloc.py
git commit -m "ar: deduplicate active payment status"
```

---

### Task 4: Soft-Delete and Attribute AR Follow-Up History

**Files:**
- Create: `data/migrations/159_ar_followup_soft_delete.sql`
- Create: `data/migrations/159_ar_followup_soft_delete.rollback.sql`
- Modify: `data/schema.sql` via `scripts/dump_schema.py`
- Modify: `inventory_app/ar_followup.py:125-207,377-534`
- Modify: `inventory_app/blueprints/accounting.py:683-694`
- Test: `tests/test_ar_followup.py`
- Test: `tests/test_ar_page.py`
- Create: `tests/test_mig159_ar_followup_soft_delete.py`

**Interfaces:**
- Produces columns `deleted_at TEXT`, `deleted_by TEXT` on `ar_followup_log`.
- Produces `delete_outreach(log_id: int, deleted_by: str, conn=None, db_path=None) -> bool`.
- All follow-up readers exclude `deleted_at IS NOT NULL`.

- [ ] **Step 1: Have db-architect write migration and rollback tests first**

Forward migration:

```sql
BEGIN;
ALTER TABLE ar_followup_log ADD COLUMN deleted_at TEXT;
ALTER TABLE ar_followup_log ADD COLUMN deleted_by TEXT;
CREATE INDEX idx_ar_followup_active_customer
    ON ar_followup_log(customer_code, customer, log_date DESC)
    WHERE deleted_at IS NULL;
COMMIT;
```

Do not self-stamp `applied_migrations`; the migration runner must write the filename, checksum, and duration. Rollback must use native SQLite operations on the current table so rows inserted after migration survive:

```sql
BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_ar_followup_active_customer;
ALTER TABLE ar_followup_log DROP COLUMN deleted_by;
ALTER TABLE ar_followup_log DROP COLUMN deleted_at;
DELETE FROM applied_migrations
 WHERE filename='159_ar_followup_soft_delete.sql';
COMMIT;
```

The migration test must reconstruct pre-159 state before applying, then prove forward columns/index, post-forward inserted-row survival through rollback, original three-index survival, and removal of the runner's migration record.

- [ ] **Step 2: Write failing model/route tests**

Test that deleting records `deleted_at` and actor, hides the row from ranking/history/overdue readers, preserves the physical row, returns `True` once and `False` on repeated/nonexistent deletion, and the route does not flash success when `False`.

- [ ] **Step 3: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_mig159_ar_followup_soft_delete.py \
  tests/test_ar_followup.py tests/test_ar_page.py -q
```

- [ ] **Step 4: Implement the soft-delete contract**

Use:

```python
cur = c.execute(
    """UPDATE ar_followup_log
          SET deleted_at=datetime('now','localtime'), deleted_by=?
        WHERE id=? AND deleted_at IS NULL""",
    (deleted_by, log_id),
)
return cur.rowcount == 1
```

Add `deleted_at IS NULL` to every ranking/log/detail/overdue query and subquery. Route actor is `session.get('username')` (not display text). Flash success only for `True`; otherwise warn that the row was already removed or missing.

- [ ] **Step 5: Regenerate and verify schema**

```bash
~/.virtualenvs/erp/bin/python scripts/dump_schema.py
~/.virtualenvs/erp/bin/pytest \
  tests/test_mig159_ar_followup_soft_delete.py \
  tests/test_fresh_db_build.py \
  tests/test_migration_numbering.py -q
```

- [ ] **Step 6: Mutation checks, independent review, and commit**

Delete each of the active-row filters one at a time and prove ranking, history, and overdue tests each go red. Review migration/rollback fidelity and audit attribution. Commit:

```bash
git add data/migrations/159_ar_followup_soft_delete.sql \
  data/migrations/159_ar_followup_soft_delete.rollback.sql data/schema.sql \
  inventory_app/ar_followup.py inventory_app/blueprints/accounting.py \
  tests/test_ar_followup.py tests/test_ar_page.py \
  tests/test_mig159_ar_followup_soft_delete.py
git commit -m "ar: preserve deleted follow-up history"
```

---

### Task 5: Resolve Follow-Up Identity Server-Side and Reject Invalid Inputs

**Files:**
- Modify: `inventory_app/ar_followup.py:261-292,383-411`
- Modify: `inventory_app/blueprints/accounting.py:635-680`
- Modify: `inventory_app/templates/ar_followup_detail.html:47-101`
- Test: `tests/test_ar_followup.py`
- Test: `tests/test_ar_page.py`

**Interfaces:**
- Produces: `resolve_customer_target(target, conn=None, db_path=None) -> {'customer_code': str|None, 'customer': str}|None`.
- Route consumes only `customer_key` as identity; posted customer name/code are not authoritative.

- [ ] **Step 1: Write failing identity and validation tests**

Post `customer_key=C-A` with forged `customer=C-B name` and `customer_code=C-B`; assert the stored log uses server-resolved C-A identity. Test unknown key refuses without insert. Test `promised_amount='abc'` and `-1` refuse without insert; comma-formatted positive input stores correctly. Test malformed ISO dates refuse without insert.

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_ar_followup.py tests/test_ar_page.py -q
```

- [ ] **Step 3: Implement one stable identity input**

Expose a small wrapper over `_resolve_target()` that resolves the canonical code and chooses the current master name first, then newest snapshot name, then newest existing log name. Return `None` if no customer/snapshot/log evidence exists. In the route, ignore hidden `customer` and `customer_code`; resolve only `customer_key`.

Parse money fail-loud:

```python
raw = (request.form.get('promised_amount') or '').strip()
try:
    promised_amount = float(raw.replace(',', '')) if raw else None
except ValueError:
    flash('ยอดที่นัดจ่ายต้องเป็นตัวเลข', 'danger')
    return redirect(url_for('accounting.ar_followup_customer',
                            customer_key=customer_key))
if promised_amount is not None and promised_amount < 0:
    flash('ยอดที่นัดจ่ายห้ามติดลบ', 'danger')
    return redirect(url_for('accounting.ar_followup_customer',
                            customer_key=customer_key))
```

Validate each non-empty date with `date.fromisoformat()`. Preserve channel/result CHECK constraints as the final enum guard.

- [ ] **Step 4: Run GREEN and mutation controls**

Run Task 5 tests. Restore trust in posted customer fields once and verify the forged-identity test fails; restore. Change invalid amount fallback to `None` and verify invalid-money test fails; restore.

- [ ] **Step 5: Independent review and commit**

Review same-name/different-code behavior, orphan handling, redirects, and no partial insert. Commit:

```bash
git add inventory_app/ar_followup.py inventory_app/blueprints/accounting.py \
  inventory_app/templates/ar_followup_detail.html \
  tests/test_ar_followup.py tests/test_ar_page.py
git commit -m "ar: validate follow-up identity and promises"
```

---

### Task 6: Import Every Express Customer-Code Shape and Refuse Zero/Partial Parses

**Files:**
- Modify: `inventory_app/blueprints/partners.py:243-333`
- Test: `tests/test_route_hygiene_b567.py`
- Test: `tests/test_customer_import_hardening.py`

**Interfaces:**
- Produces: `_parse_bsn_customers() -> list[dict]` on a valid complete parse.
- Accepts: the code token Express actually prints in the customer-code column; code grammar is not restricted beyond a non-empty, whitespace-free token containing at least one digit.
- Raises: `ValueError` containing source-line context for a zero/partial customer-candidate parse.

- [ ] **Step 1: Write failing parser tests with real cp874 files**

Use real line shapes from the current Express export. Add one fixture containing ordinary codes plus `L1004ค01`, `1101ค01`, and `23ทธ01`; assert all rows import with their exact code unchanged and their own address, phone, contact, and credit-days values. Include a genuine continuation/address line between customers so the test proves it is not classified as a new customer.

Add a malformed structural customer row (code/name present but missing the salesperson/zone/sequence tail) and assert `ValueError` names that source line. Add a report-shaped file with the correct title but zero customer rows and assert refusal. Add a non-report file and assert refusal.

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_route_hygiene_b567.py \
  tests/test_customer_import_hardening.py -q
```

- [ ] **Step 3: Parse the structural row, not an invented code grammar**

Before the loop, verify the report contains `รายงาน` and `ลูกค้า`. Replace the code-specific matcher with one structural customer-row matcher used both to start a customer and to stop the preceding customer's continuation scan:

```python
customer_row_re = re.compile(
    r'  (\S+)\s+(.+?)\s{3,}(\S+)\s+(\S+)\s+\d+'
)
```

After a match, validate only that the captured code is whitespace-free and contains at least one digit. Do not constrain where digits or Thai/Latin letters occur. The continuation loop must break on `customer_row_re.match(nl)`—not on the old narrow code regex—so a newly supported code cannot have its phone/address/contact absorbed into the previous customer.

Track looser customer-shaped rows separately: a non-heading line beginning with two spaces, a non-whitespace first token containing a digit, and text in the customer-name area, but failing `customer_row_re`. Append `(line_number, excerpt)` to `rejected`. Explicitly exempt address/phone/Tax ID/credit/page/header continuation lines; the real continuation-row fixture must exercise that exemption. After parsing:

```python
if rejected:
    line_no, excerpt = rejected[0]
    raise ValueError(
        f'อ่านข้อมูลลูกค้าไม่ครบ: บรรทัด {line_no}: {excerpt[:120]}'
    )
if not customers:
    raise ValueError('ไม่พบรายการลูกค้าในรายงาน — ไฟล์ผิดประเภทหรือรูปแบบเปลี่ยน')
```

Catch `ValueError` in `customer_import_bsn()`, flash the exact actionable message, and do not call `import_customers_from_bsn()`.

- [ ] **Step 4: Run GREEN and parser mutation matrix**

Run Task 6 tests. Independently restore the old narrow code matcher, remove the shared continuation break, remove header validation, remove rejected-candidate refusal, and remove zero-parse refusal; each corresponding control must fail for its own reason. Restore and rerun.

- [ ] **Step 5: Independent review and commit**

Run the parser on the current real Express customer source and compare old/new output by code. Required evidence: all **284** previously omitted letter-/multi-letter-shaped Express codes now appear, no formerly parsed code disappears, and each newly included customer's contact/address/phone/credit fields terminate at its own row. Review false positives against headings/page breaks before committing.

```bash
git add inventory_app/blueprints/partners.py \
  tests/test_route_hygiene_b567.py tests/test_customer_import_hardening.py
git commit -m "customers: refuse incomplete master imports"
```

---

### Task 7: Harden Invoice Pagination Without Expanding Scope

**Files:**
- Modify: `inventory_app/blueprints/accounting.py:313-333`
- Test: `tests/test_ar_page.py`

**Interfaces:**
- Produces: invalid, zero, or negative `page` resolves to page 1 without HTTP 500.

- [ ] **Step 1: Add failing route contracts**

```python
@pytest.mark.parametrize('raw', ['abc', '0', '-2'])
def test_ar_invoice_page_invalid_values_fall_back_to_one(tmp_db, raw):
    response = _admin(tmp_db).get(f'/ar?tab=invoices&page={raw}')
    assert response.status_code == 200
    assert 'หน้า 1/' in response.get_data(as_text=True)
```

- [ ] **Step 2: Run RED**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_ar_page.py -q
```

- [ ] **Step 3: Use Flask's typed getter and clamp**

```python
page = max(request.args.get('page', 1, type=int) or 1, 1)
```

- [ ] **Step 4: Run GREEN, review, and commit**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_ar_page.py -q
git add inventory_app/blueprints/accounting.py tests/test_ar_page.py
git commit -m "ar: sanitize invoice page parameter"
```

---

### Task 8: Full Verification, Running-App Proof, and Codex Handoff

**Files:**
- Do not modify tracker files.
- Do not add production scripts or data extracts to the branch.

**Interfaces:**
- Produces: review-ready branch, exact evidence, and no merge/deploy.

- [ ] **Step 1: Run all focused and adjacent tests**

Run one combined command so no file is double-counted:

```bash
~/.virtualenvs/erp/bin/pytest -q \
  tests/test_ar_page.py tests/test_ar_followup.py tests/test_ar_reconcile.py \
  tests/test_ar_writeoffs.py tests/test_payments_alloc.py \
  tests/test_payment_parse.py tests/test_import_payments_b2a_regression.py \
  tests/test_credit_notes_import.py tests/test_credit_notes_parse.py \
  tests/test_credit_notes_preview.py tests/test_customer_credit_rows.py \
  tests/test_customer_import_hardening.py \
  tests/test_customer_import_preserves_reassignment.py \
  tests/test_customer_edit_modal.py tests/test_customer_audit_history_card.py \
  tests/test_customer_code_route.py tests/test_customer_unpaid_bills_snapshot_date.py \
  tests/test_customers_search_symmetry.py tests/test_call_card.py \
  tests/test_call_card_render.py tests/test_call_routes.py \
  tests/test_customer_review_routes.py tests/test_express_ap_ar_import.py \
  tests/test_unified_import.py tests/test_express_dbf_source.py \
  tests/test_express_dbf_upload_route.py tests/test_accounting_summary_v2.py \
  tests/test_cashflow_route.py tests/test_commission_customer_reassign.py \
  tests/test_customer_geo.py tests/test_customer_geomap_import.py \
  tests/test_customer_contact_normalize.py tests/test_post_whitelist.py \
  tests/test_endpoint_module_coverage.py tests/test_nav_split_trade_finance.py \
  tests/test_route_hygiene_b567.py tests/test_mig159_ar_followup_soft_delete.py \
  tests/test_fresh_db_build.py tests/test_migration_numbering.py
```

The pre-change review set was 536 passed / 3 mounted-fixture skips. Record the new exact passed/skipped/failed counts and skipped reasons; do not require the old count after adding tests.

- [ ] **Step 2: Run mutation controls on final code**

Prove each guard fails independently:

1. AR stale threshold removed.
2. receipt child deletion disabled.
3. active-payment `DISTINCT` removed.
4. cancelled-receipt predicate removed.
5. soft-delete reader filter removed.
6. follow-up identity trusts form values.
7. customer-parser partial rejection removed.

Restore after each mutation and rerun the corresponding test green.

- [ ] **Step 3: Boot a real app against a disposable clone with CSRF on**

Use `sqlite3 .backup` outside the repository. Start the worktree app with its data directory pointed only at that clone. Verify authenticated HTTP flows:

1. All four `/ar` tabs return 200 and display the stale snapshot warning.
2. `/accounting/ar-followup/customer/<known-code>` returns 200 and displays the same warning.
3. Valid follow-up insert persists canonical identity; forged hidden identity does not change it.
4. Invalid/negative promise amount writes no row.
5. Delete soft-deletes with username and disappears from UI while remaining in SQL.
6. Customer-master malformed/zero file refuses with no customer mutation.
7. Re-importing the crafted receipt v2 removes its v1-only child.
8. Invoice summary satisfies count invariants and never exceeds 100%.
9. Importing current read-only `ARRCPIT` data into the clone produces exactly the source-owned links for `IV6900907` and `IV6900976`.
10. The current real customer export imports all 284 previously omitted Express codes without cross-customer field bleed.

Capture clone hash and row-count baselines. Confirm the live/local business DB hash remains unchanged.

- [ ] **Step 4: Run independent final code review**

Ask `code-reviewer` to review the complete diff against freshly fetched `origin/main`, focusing on money grain, cancellation semantics, transition of existing stale links, migration/rollback fidelity, source freshness semantics, permissions, audit attribution, and tests that can fail for the intended reason. Resolve every BLOCKER/IMPORTANT item and repeat Steps 1–3.

- [ ] **Step 5: Rebase safety check**

Fetch origin again. If `origin/main` moved, rebase, rerun migration-number collision check and the complete verification. Confirm the diff does not revert Hammer Pack or payroll changes.

- [ ] **Step 6: Stop and report to Codex**

Report:

- branch, worktree, base commit, and final commit;
- changed files and migration number;
- RED evidence per finding;
- GREEN test counts and three skipped-fixture disposition;
- mutation matrix results;
- running-app HTTP scenarios and disposable DB path/hash;
- business DB before/after hash;
- the authoritative `ARRCPIT` rows and resolved owner receipt for each disputed ฿35 link;
- real customer-source before/after code-set counts, including all 284 newly supported codes;
- independent-review verdict and every resolved follow-up;
- remaining gaps or business decisions.

Do **not** merge, deploy, push production data, delete the worktree, or mark the tracker ✅. Codex owns final review and production verification.
