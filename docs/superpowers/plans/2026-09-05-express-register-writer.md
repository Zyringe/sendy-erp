# Express Register Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace three duplicated Express full-register writers and the remaining derived register lists with one executable register vocabulary and one guarded replacement writer, without changing import results or operator policy.

**Architecture:** A new `express_registers.py` module owns immutable `Register` descriptors, the full register tuple, derived key/snapshot views, and `replace(key, record_groups, entity, db_path)`. The replacement interface keeps the existing `BEGIN IMMEDIATE`, child-first delete order, empty-parse refusal, entity scoping, and return counts. `import_router.py` continues to own DBF parsing/orchestration and keeps billing-note and invoice-reference upserts explicit because upsert semantics are materially different from full-register replacement.

**Tech Stack:** Python 3.9, dataclasses, sqlite3, pytest

**Spec:** `/Users/putty/Sendai-Boonsawat/Operations/05_analysis-reports/engineering/architecture-review-tiktok-erp_2026-09-04.html#c7`

## Global Constraints

- No schema or migration changes.
- Preserve the exact keys and Thai labels currently exposed through `commit_express_dbf()`.
- Preserve empty-first-import success and empty-later-import refusal for GL, sales orders, and bank cheques.
- Preserve one `BEGIN IMMEDIATE` transaction per replaced register, entity-scoped deletion, and rollback-on-error.
- Do not fold billing-note or invoice-reference upserts into replacement semantics.
- Tests use temporary SQLite databases only; do not copy or mutate the live development database.

---

### Task 1: Add the deep Express-register module and migrate the three replacements

**Files:**
- Create: `inventory_app/express_registers.py`
- Create: `tests/test_express_registers.py`
- Modify: `inventory_app/import_router.py:325-797`

**Interfaces:**
- Consumes: SQLite schemas already created by the test `empty_db` fixture; record dictionaries produced by `express_dbf_source`.
- Produces: `Register`, `REGISTERS`, `REGISTER_KEYS`, `SNAPSHOT_KEYS`, and `replace(key, record_groups, entity, db_path) -> tuple`.

- [x] **Step 1: Write the failing interface and behavior tests**

Add tests that import `express_registers` and prove, through real SQLite writes:

```python
def test_replace_bank_cheques_replaces_and_returns_count(empty_db):
    counts = express_registers.replace(
        'bank_cheques', ([BANK_CHEQUE_RECORD],), 'BSN', empty_db)
    assert counts == (1,)
    assert query_count(empty_db, 'express_bank_cheques', 'BSN') == 1


def test_empty_replacement_refuses_to_erase_stored_rows(empty_db):
    express_registers.replace(
        'bank_cheques', ([BANK_CHEQUE_RECORD],), 'BSN', empty_db)
    with pytest.raises(ValueError, match='refusing to erase'):
        express_registers.replace('bank_cheques', ([],), 'BSN', empty_db)
    assert query_count(empty_db, 'express_bank_cheques', 'BSN') == 1


def test_multitable_replacement_rolls_back_every_delete_on_bad_record(empty_db):
    express_registers.replace(
        'sales_orders', ([ORDER_RECORD], [LINE_RECORD]), 'BSN', empty_db)
    with pytest.raises(KeyError):
        express_registers.replace(
            'sales_orders', ([NEW_ORDER_RECORD], [{'so_no': 'SO-BROKEN'}]),
            'BSN', empty_db)
    assert stored_order_numbers(empty_db) == ['SO-OLD']
```

Use literal complete records matching the production schema. The mutation each test catches is respectively: wrong table/column/count mapping, deletion before the empty guard, and a commit or non-atomic delete/insert sequence.

- [x] **Step 2: Run the new tests and verify RED**

Run:

```bash
~/.virtualenvs/erp/bin/pytest tests/test_express_registers.py -q
```

Expected: FAIL during collection because `express_registers` does not exist.

- [x] **Step 3: Implement the minimum register interface**

Create immutable descriptors whose storage portion records insert-order tables/columns, child-first delete order, the guard table/group, and the existing empty-error noun. Implement `replace()` so it:

```python
def replace(key, record_groups, entity, db_path):
    register = _BY_KEY[key]
    storage = register.storage
    # validate group count before opening a transaction
    # inspect the configured guard group before any DELETE
    # BEGIN IMMEDIATE
    # DELETE configured tables child-first for this entity
    # INSERT configured groups in parent-first order
    # commit, close, and return one count per group
```

Only `general_ledger`, `sales_orders`, and `bank_cheques` receive replacement storage specs. Keep SQL identifiers internal and hard-coded in the descriptors; values remain parameterized.

- [x] **Step 4: Migrate production calls and delete the duplicate writers**

In `commit_express_dbf()`, replace calls to `_replace_general_ledger`, `_replace_sales_orders`, and `_replace_bank_cheques` with `express_registers.replace(...)`, unpacking the returned tuple into the same existing result dictionaries. Delete the three old writer functions. Leave `_upsert_billing_notes` and `_upsert_invoice_refs` unchanged.

- [x] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_express_registers.py \
  tests/test_express_general_ledger.py \
  tests/test_express_sales_orders.py \
  tests/test_express_bank_cheques.py -q
```

Expected: all pass; the existing result shapes remain unchanged.

- [x] **Step 6: Commit the executable register writer**

```bash
git add inventory_app/express_registers.py inventory_app/import_router.py tests/test_express_registers.py
git commit -m "refactor: centralize Express register replacement"
```

---

### Task 2: Move every register consumer onto the executable vocabulary

**Files:**
- Modify: `inventory_app/import_router.py:317-339`
- Modify: `inventory_app/blueprints/bsn.py:538-540,1368`
- Modify: `inventory_app/vat_book_builder.py:238-239`
- Modify: `tests/test_express_dbf_upload_multibook.py:977-1027`

**Interfaces:**
- Consumes: `express_registers.REGISTERS`, `REGISTER_KEYS`, and `SNAPSHOT_KEYS` from Task 1.
- Produces: all register warning, result-coverage, and VAT snapshot consumers derive from the same descriptors; `import_router` no longer declares parallel register constants.

- [x] **Step 1: Write the failing consumer-contract test**

Replace the current identity assertion against `import_router.ISOLATED_REGISTERS` with behavior over the new descriptor interface:

```python
def test_every_register_consumer_reads_the_executable_vocabulary():
    assert bsn._ISOLATED_REGISTERS is express_registers.REGISTERS
    assert set(vat_book_builder._SNAPSHOT_LABELS) == set(express_registers.SNAPSHOT_KEYS)
    assert len(express_registers.REGISTERS) == 6
```

Update the real-import-result coverage test to use `express_registers.REGISTER_KEYS`. The first assertion fails while the blueprint still reads the old tuple.

Execution note: this identity assertion produced the expected RED. Pre-commit review then removed both the private blueprint alias and its structural assertion; the final route iterates `express_registers.REGISTERS` directly, while behavior tests cover warning rendering and real-result key coverage.

- [x] **Step 2: Run the consumer tests and verify RED**

Run:

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_express_dbf_upload_multibook.py::test_every_register_consumer_reads_the_executable_vocabulary \
  tests/test_express_dbf_upload_multibook.py::test_every_declared_register_key_exists_in_a_real_import_result -q
```

Expected: FAIL because `bsn`, `import_router`, or `vat_book_builder` still owns/reads the previous constants.

- [x] **Step 3: Migrate consumers and remove parallel declarations**

Import `express_registers` where needed. Have the blueprint iterate descriptor attributes (`register.key`, `register.label`, `register.stale_hint`), derive VAT labels from descriptors marked `snapshot_required`, and have `import_router` use the canonical derived keys. Remove `ISOLATED_REGISTERS`, `ISOLATED_REGISTER_KEYS`, and `SNAPSHOT_REGISTER_KEYS` from `import_router` once no consumer reads them.

- [x] **Step 4: Run the Express register/upload suite and verify GREEN**

Run:

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_express_registers.py \
  tests/test_express_general_ledger.py \
  tests/test_express_sales_orders.py \
  tests/test_express_bank_cheques.py \
  tests/test_express_dbf_upload_multibook.py \
  tests/test_express_doc_drift_wiring.py \
  tests/test_express_doc_drift_skip.py -q
```

Expected: all pass with no warning/error output.

- [x] **Step 5: Commit the single vocabulary migration**

```bash
git add inventory_app/import_router.py inventory_app/blueprints/bsn.py \
  inventory_app/vat_book_builder.py tests/test_express_dbf_upload_multibook.py
git commit -m "refactor: derive Express register consumers from registry"
```

---

### Task 3: Regression and running-app verification

**Files:**
- Modify only if verification exposes a C7 regression.

**Interfaces:**
- Consumes: the completed C7 module and unchanged `/import-express-dbf/upload` route.
- Produces: evidence that C7 preserves the current application contract.

- [x] **Step 1: Run all Express tests**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_express_*.py -q
```

Expected: all pass.

Result: `308 passed in 60.97s`.

- [x] **Step 2: Run the full Sendy suite**

```bash
~/.virtualenvs/erp/bin/pytest -q
```

Expected: no new failures relative to a fresh baseline in this same worktree.

Result: `5119 passed, 78 skipped, 10 failed, 39 errors`. Targeted runs on
unchanged `main` reproduced the migration-150 errors and nine unrelated
failures. The remaining VAT-view errors come from that test module hard-coding
the isolated worktree's absent `inventory_app/instance/inventory.db`; the shared
checkout's copy passes those tests. No failure exercises C7 code.

- [x] **Step 3: Run static diff checks**

```bash
git diff --check origin/main...HEAD
git status --short
```

Expected: no whitespace errors; only C7 code, tests, and this plan are changed.

Result: `git diff --check origin/main...HEAD` is clean. The only worktree change
left for the verification commit is a comment-only clarification in
`express_registers.py` plus this evidence update.

- [x] **Step 4: Boot the branch and exercise the unchanged route boundary**

Run the worktree code against a disposable `.backup` database, log in with the existing local test flow, GET `/import-express-dbf`, and POST a controlled test zip through `/import-express-dbf/upload`. Verify non-500 responses and that the rendered result contains the same register keys/labels. Do not use or mutate `inventory_app/instance/inventory.db` from the shared checkout.

Result: Gunicorn booted against a disposable SQLite `.backup`; `/healthz`
returned 200, the unauthenticated import page redirected to login, authenticated
GET returned 200, and both the no-file POST and a harmless non-DBF zip POST
redirected without a 500. The shared development database was not mutated.

- [x] **Step 5: Record verification in the architecture review before integration**

Update the C7 card in `/Users/putty/Sendai-Boonsawat/Operations/05_analysis-reports/engineering/architecture-review-tiktok-erp_2026-09-04.html` only after the branch verification is complete, using exact test counts and commit hashes from this run.
