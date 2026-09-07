# Payroll Note Clearing Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an admin to clear existing payroll addition and deduction notes by submitting blank note fields in the payroll-item edit form.

**Architecture:** Keep the existing model contract: `None` means “field omitted; preserve the current value,” while `""` means “explicitly clear the note.” Fix only the Flask route boundary so it preserves that distinction instead of collapsing both cases to `None`. No schema, template, or money-calculation change is needed.

**Tech Stack:** Python 3.9, Flask 3.x, SQLite, pytest.

**Spec:** `/Users/putty/Sendai-Boonsawat/projects/codex-review/README.md` — HR & Payroll row dated 2026-08-15.

## Global Constraints

- Read `/Users/putty/Sendai-Boonsawat/CLAUDE.md`, `/Users/putty/Sendai-Boonsawat/references/workspace-operating-manual.md`, `/Users/putty/Sendai-Boonsawat/sendy_erp/CLAUDE.md`, and `/Users/putty/Sendai-Boonsawat/.claude/rules/erp-engineering-discipline.md` before editing.
- Fetch `origin`, then create an isolated worktree from fresh `origin/main`; suggested branch: `fix/payroll-note-clearing`. The shared Sendy checkout contains an unrelated untracked Hammer Pack plan and must remain untouched.
- TDD is mandatory: demonstrate RED on current `origin/main`, then GREEN after the smallest implementation.
- Do not add a migration, dependency, helper abstraction, template change, or model-layer behavior change.
- Preserve partial-POST behavior: an absent note key must still pass `None` and leave the stored note unchanged; a present blank note key must pass `""` and clear it.
- Tests must use the existing disposable `tmp_db` fixture. Confirm the worktree cannot reach the live/local business database through a symlink or shared path before running tests.
- Run an independent read-only code review before committing. Resolve every blocker/important finding and re-run affected tests.
- Stop before merge, deployment, production mutation, or tracker promotion. The tracker stays 🟠 until Codex independently verifies the deployed UI on production.

---

### Task 1: Pin the Route Contract With a Failing Regression Test

**Files:**
- Modify: `tests/test_bp_hr_routes.py`

**Interfaces:**
- Consumes: existing `admin_client`, `tmp_db`, and `_make_finalized_run(tmp_db)` fixtures/helpers.
- Produces: route-level regression coverage proving the difference between an absent note field and an explicitly blank note field.

- [ ] **Step 1: Add the explicit-clear regression test**

Add this test near the other payroll route tests:

```python
def test_payroll_item_edit_clears_explicitly_blank_notes(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    admin_client.post(
        f'/hr/payroll/{rid}/reopen',
        data={'reason': 'prepare note-clear route test'},
    )

    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? ORDER BY id LIMIT 1",
            (rid,),
        ).fetchone()[0]
        conn.execute(
            """UPDATE payroll_items
                  SET other_additions_note='temporary addition note',
                      other_deductions_note='temporary deduction note'
                WHERE id=?""",
            (item_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin_client.post(
        f'/hr/payroll/{rid}/item/{item_id}',
        data={
            'other_additions_note': '',
            'other_deductions_note': '',
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    try:
        notes = conn.execute(
            """SELECT other_additions_note, other_deductions_note
                 FROM payroll_items WHERE id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    assert notes == ('', '')
```

- [ ] **Step 2: Add the omitted-field preservation control**

Add a second test so the fix cannot turn missing form keys into destructive clears:

```python
def test_payroll_item_edit_preserves_notes_when_keys_are_omitted(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    admin_client.post(
        f'/hr/payroll/{rid}/reopen',
        data={'reason': 'prepare omitted-note route test'},
    )

    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? ORDER BY id LIMIT 1",
            (rid,),
        ).fetchone()[0]
        conn.execute(
            """UPDATE payroll_items
                  SET other_additions_note='keep addition note',
                      other_deductions_note='keep deduction note'
                WHERE id=?""",
            (item_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin_client.post(
        f'/hr/payroll/{rid}/item/{item_id}',
        data={'bonus': '0'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    try:
        notes = conn.execute(
            """SELECT other_additions_note, other_deductions_note
                 FROM payroll_items WHERE id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    assert notes == ('keep addition note', 'keep deduction note')
```

- [ ] **Step 3: Run RED and record the exact failure**

Run:

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_bp_hr_routes.py::test_payroll_item_edit_clears_explicitly_blank_notes \
  tests/test_bp_hr_routes.py::test_payroll_item_edit_preserves_notes_when_keys_are_omitted \
  -v
```

Expected on unmodified `origin/main`:

- `test_payroll_item_edit_clears_explicitly_blank_notes` fails because both stored notes remain the seeded text.
- `test_payroll_item_edit_preserves_notes_when_keys_are_omitted` passes.

Do not proceed unless the first test fails for that exact behavioral reason rather than fixture/setup failure.

---

### Task 2: Preserve Blank Versus Missing at the Flask Boundary

**Files:**
- Modify: `inventory_app/blueprints/hr.py:690-695`
- Test: `tests/test_bp_hr_routes.py`

**Interfaces:**
- Consumes: `hr_mod.update_payroll_item(..., other_additions_note: Optional[str], other_deductions_note: Optional[str])`, where `None` preserves and `""` clears.
- Produces: route arguments that retain the submitted-field distinction.

- [ ] **Step 1: Make the minimum two-line implementation change**

Replace only the two note arguments:

```python
        hr_mod.update_payroll_item(
            item_id,
            bonus=_float_or_none("bonus"),
            other_additions=_float_or_none("other_additions"),
            other_additions_note=request.form.get("other_additions_note"),
            other_deductions=_float_or_none("other_deductions"),
            other_deductions_note=request.form.get("other_deductions_note"),
            wht_amount=_float_or_none("wht_amount"),
            late=_bool_or_none("late"),
        )
```

Rationale:

- Missing key → Flask returns `None` → model preserves the existing note.
- Present blank key → Flask returns `""` → model writes an explicit blank.
- Present text → Flask returns the text unchanged.

- [ ] **Step 2: Run GREEN on both contract tests**

Run the same two-test command from Task 1.

Expected: `2 passed`.

- [ ] **Step 3: Run the complete route test file**

Run:

```bash
~/.virtualenvs/erp/bin/pytest tests/test_bp_hr_routes.py -v
```

Expected: all tests pass with zero failures/errors.

- [ ] **Step 4: Run the focused HR/payroll regression suite**

Run:

```bash
~/.virtualenvs/erp/bin/pytest \
  tests/test_hr_payroll.py \
  tests/test_bp_hr_routes.py \
  tests/test_hr_wht_ui.py \
  tests/test_hr_salary_advance.py \
  tests/test_salary_pay_event.py \
  -q
```

Expected: zero failures/errors. Record the exact pass count.

- [ ] **Step 5: Run a local HTTP check against a disposable database clone**

Use `sqlite3 .backup` to create a disposable database outside the repository. Start the app with its database path redirected to that clone, then use an authenticated Flask client or browser to:

1. Open a draft payroll item.
2. Save unique text in both note fields with zero-valued addition/deduction amounts.
3. Reopen the editor and confirm both texts persisted.
4. Submit both note fields blank.
5. Reopen the editor and confirm both fields are blank.
6. Confirm run status, employee count, gross/net values, and run total did not change.

Also verify the worktree/app process never opened `/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db` for writing.

- [ ] **Step 6: Run independent read-only review before commit**

Ask the `code-reviewer` to review only the diff against fresh `origin/main`, focusing on:

- whether blank and missing keys are correctly distinguished;
- whether any money field or finalized-run guard changed;
- whether the tests can fail on the original bug;
- whether the disposable-DB boundary is real.

Fix every blocker/important finding, then re-run Steps 2–5.

- [ ] **Step 7: Commit the minimal change**

```bash
git add inventory_app/blueprints/hr.py tests/test_bp_hr_routes.py
git commit -m "hr: allow clearing payroll item notes"
```

---

### Task 3: Hand Back for Codex Review and Production Gate

**Files:**
- Do not modify `projects/codex-review/README.md` in Claude's branch.
- Do not modify `/Users/putty/Sendai-Boonsawat/projects/README.md` in Claude's branch.

**Interfaces:**
- Consumes: committed two-file diff and verification evidence from Tasks 1–2.
- Produces: a review-ready handoff; Codex owns tracker promotion after production verification.

- [ ] **Step 1: Report the implementation evidence**

Claude must report:

- branch and worktree paths;
- changed files and commit SHA;
- RED failure output proving the regression test caught the bug;
- GREEN pass counts for the two contract tests, route file, and focused HR suite;
- disposable database path and proof it was not the live/local business DB;
- local HTTP before/edit/clear/after values;
- independent-review verdict and any resolved findings;
- remaining gaps.

- [ ] **Step 2: Stop before merge/deploy**

Do not merge, deploy, mutate production, or mark HR ✅. Put will ask Codex to independently review the diff and verification evidence first.

- [ ] **Step 3: Production verification after approved deployment**

After deployment, Codex—not Claude—will perform a production-safe check on draft run #8:

1. Record status, employee count, and total.
2. Put unique text in both note fields for one zero-valued payroll item and save.
3. Reopen the editor and confirm both notes persisted.
4. Clear both fields and save.
5. Reopen the editor and confirm both are blank.
6. Confirm status, employee count, payroll values, and total equal the baseline.

Only after this passes may Codex change HR & Payroll from 🟠 to ✅ in the review tracker.
