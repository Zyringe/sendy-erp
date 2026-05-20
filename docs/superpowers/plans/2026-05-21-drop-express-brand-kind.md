# Drop `express_sales.brand_kind` Cache — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End the write-only `express_sales.brand_kind` cache as a class of bug. Drop the column, drop the trigger that kept it fresh, strip writers from import scripts, clean up dead comments and tests. Validated by byte-identical `get_commission_for_month` output for last 3 months pre- vs post-drop.

**Architecture:** Single mig 068 drops trigger → drops index → drops column. Writer scripts and stale comments are cleaned in code commits BEFORE the mig runs (so the mig only fails late if we miss a writer). Read paths are unchanged — PR #33 already ships the CASE derive that makes the cache redundant.

**Tech Stack:** SQLite ≥ 3.35.0 (DROP COLUMN support), Python 3.9, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-21-drop-express-brand-kind-design.md` (committed `59bcb97`).

---

## File Structure

- **Create:** `data/migrations/068_drop_express_sales_brand_kind.sql` + `.rollback.sql`
- **Modify:**
  - `scripts/import_express.py` — remove writer
  - `scripts/load_brand_map.py` — remove recompute step
  - `scripts/backfill_express_unit_normalize.py` — remove recompute step
  - `inventory_app/commission.py` — clean dead comments (read path unchanged)
  - `inventory_app/models.py:176` — docstring cleanup
  - `inventory_app/templates/commission_invoice_detail.html:117-120` — hint text cleanup
  - `tests/test_commission_unit_aware.py` — remove cache-staleness regression suite (lines ~276-357)
  - `tests/test_migration_061_mapping_unit_aware.py` — drop the brand_kind assertion (line ~226)
- **Delete:** `scripts/isolate_issue30_impact.py` (issue #30 closed; the script reads the dead cache)

---

## Task 1: Capture commission baseline (pre-change snapshot)

**Files:** none — produces `/tmp/commission_baseline.json` for later equivalence check.

- [ ] **Step 1: Create the baseline-capture script**

Create `/tmp/capture_commission_baseline.py`:

```python
"""Snapshot get_commission_for_month for the last 3 months pre-drop."""
import json
import os
import shutil
import sqlite3
import sys

SENDY = "/Users/putty/Sendai-Boonsawat/sendy_erp"
sys.path.insert(0, os.path.join(SENDY, "inventory_app"))
sys.path.insert(0, SENDY)

LIVE_DB = os.path.join(SENDY, "inventory_app", "instance", "inventory.db")
TMP_DB  = "/tmp/commission_baseline.db"

print(f"[baseline] copying {LIVE_DB} → {TMP_DB}")
shutil.copy2(LIVE_DB, TMP_DB)
for suffix in ("-wal", "-shm"):
    src = LIVE_DB + suffix
    if os.path.exists(src):
        shutil.copy2(src, TMP_DB + suffix)

import config
config.DATABASE_PATH = TMP_DB
import database
database.DATABASE_PATH = TMP_DB

from commission import get_commission_for_month

MONTHS = ["2026-03", "2026-04", "2026-05"]
baseline = {}
for ym in MONTHS:
    print(f"[baseline] computing {ym} …")
    rows = get_commission_for_month(ym, db_path=TMP_DB)
    # Normalize: rows may be list of dicts or sqlite3.Row — coerce to JSON-safe
    baseline[ym] = [
        {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
         for k, v in dict(r).items()}
        for r in rows
    ]

OUT = "/tmp/commission_baseline.json"
with open(OUT, "w") as f:
    json.dump(baseline, f, ensure_ascii=False, indent=2, sort_keys=True, default=str)
print(f"[baseline] wrote {OUT}  ({sum(len(v) for v in baseline.values())} rows total)")
```

- [ ] **Step 2: Run the capture**

```bash
cd /Users/putty/Sendai-Boonsawat/sendy_erp
~/.virtualenvs/erp/bin/python /tmp/capture_commission_baseline.py
```

Expected: prints row counts; produces `/tmp/commission_baseline.json`. If the function signature is different (e.g., requires `salesperson_code`), adjust the call but make sure the baseline includes ALL salespeople for those months. If the function name is different, grep `inventory_app/commission.py` for the canonical month-level entry point and use that.

- [ ] **Step 3: Sanity check the baseline**

```bash
~/.virtualenvs/erp/bin/python -c "
import json
b = json.load(open('/tmp/commission_baseline.json'))
for ym, rows in b.items():
    print(ym, 'rows=', len(rows))
"
```

Expected: 3 months, non-zero row counts.

This baseline is the ground truth. Do NOT regenerate it after any code change in this PR — it would defeat the equivalence check.

---

## Task 2: Strip writer in `scripts/import_express.py`

**Files:**
- Modify: `scripts/import_express.py`

- [ ] **Step 1: Read the current writer surface**

```bash
grep -n "brand_kind\|_brand_kind_for_product" scripts/import_express.py
```

Expected hits (verify): function definition around `:92`, `_brand_kind_for_product` call around `:304`, INSERT INTO express_sales lines around `:309-320` listing `brand_kind` in column tuple and `brand_kind` variable in values.

- [ ] **Step 2: Delete the helper function**

Remove the entire `def _brand_kind_for_product(...)` function block (around line 92 through its return). Also remove any inline imports it needed (likely none — uses `classify_brand_kind` from commission.py).

- [ ] **Step 3: Strip `brand_kind` from the INSERT**

Find the `INSERT INTO express_sales` block (around line 309). Remove `brand_kind` from the column list and remove the matching positional value. Example (illustrative — exact text varies):

```python
# BEFORE
cur.execute(
    """INSERT INTO express_sales
         (..., product_id, product_name_raw, brand_kind, ..., total)
       VALUES (..., ?, ?, ?, ..., ?)""",
    (..., prod_id, r.product_name, brand_kind, ..., r.total),
)

# AFTER
cur.execute(
    """INSERT INTO express_sales
         (..., product_id, product_name_raw, ..., total)
       VALUES (..., ?, ?, ..., ?)""",
    (..., prod_id, r.product_name, ..., r.total),
)
```

Remove the local `brand_kind = _brand_kind_for_product(conn, prod_id)` line above the INSERT (it's the only call site).

- [ ] **Step 4: Verify no `brand_kind` references remain in this file**

```bash
grep -n "brand_kind" scripts/import_express.py
```

Expected: zero hits.

- [ ] **Step 5: Commit**

```bash
git add scripts/import_express.py
git commit -m "fix(express): drop brand_kind writer from import_express

After PR #33, brand_kind is derived at read time from brands.is_own_brand
via _BASE_QUERY's CASE expression. The import-side cache is write-only.
Remove _brand_kind_for_product() helper + brand_kind column from the
express_sales INSERT. Prep work for mig 068 column drop."
```

---

## Task 3: Strip recompute from `scripts/load_brand_map.py`

**Files:**
- Modify: `scripts/load_brand_map.py`

- [ ] **Step 1: Survey what the script does**

```bash
head -30 scripts/load_brand_map.py
grep -n "brand_kind" scripts/load_brand_map.py
```

The script's primary purpose may be to load brand mappings (still useful) plus refresh `express_sales.brand_kind` (now dead). Preserve the former, remove the latter.

- [ ] **Step 2: Remove only the `brand_kind` refresh logic**

Identify the UPDATE statement(s) that set `express_sales.brand_kind` based on the loaded mapping, and any `_brand_kind_for_product` calls. Delete those lines + their immediate scaffolding (loop, helper imports if no longer used). Keep the brand-mapping load itself.

If the script's *sole purpose* turns out to be the brand_kind refresh (i.e., after removing those lines, no logic remains), delete the whole file and add a one-line note to the commit explaining why.

- [ ] **Step 3: Confirm no residual `brand_kind` references**

```bash
grep -n "brand_kind" scripts/load_brand_map.py
```

Expected: zero hits (or file deleted).

- [ ] **Step 4: Commit**

```bash
git add scripts/load_brand_map.py
# If deleted: git add -A
git commit -m "fix(express): drop brand_kind recompute from load_brand_map"
```

---

## Task 4: Strip recompute from `scripts/backfill_express_unit_normalize.py`

**Files:**
- Modify: `scripts/backfill_express_unit_normalize.py`

- [ ] **Step 1: Identify the recompute step**

```bash
grep -n "brand_kind" scripts/backfill_express_unit_normalize.py
```

The mig 064 backfill script also does a `brand_kind` recompute. The alias-seeding and unit-normalization parts of this script are still useful — preserve those. Strip only the `brand_kind` UPDATE.

- [ ] **Step 2: Remove the UPDATE block**

Find the `UPDATE express_sales SET brand_kind = ...` block. Delete it. Keep the surrounding unit-normalization code intact.

- [ ] **Step 3: Confirm no residual `brand_kind` references**

```bash
grep -n "brand_kind" scripts/backfill_express_unit_normalize.py
```

Expected: zero hits.

- [ ] **Step 4: Commit**

```bash
git add scripts/backfill_express_unit_normalize.py
git commit -m "fix(express): drop brand_kind recompute from backfill_express_unit_normalize"
```

---

## Task 5: Remove `scripts/isolate_issue30_impact.py` (dev script reading dead cache)

**Files:**
- Delete: `scripts/isolate_issue30_impact.py`

- [ ] **Step 1: Confirm the script is dev-only and issue #30 is closed**

```bash
head -10 scripts/isolate_issue30_impact.py
gh issue view 30 --json state --jq .state
```

Expected: script docstring describes a dev-time impact-isolation diagnostic for issue #30; issue state is `CLOSED`.

- [ ] **Step 2: Delete the file**

```bash
git rm scripts/isolate_issue30_impact.py
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: delete isolate_issue30_impact.py (issue #30 closed, reads dead cache)"
```

---

## Task 6: Clean dead comments in `inventory_app/commission.py`

**Files:**
- Modify: `inventory_app/commission.py`

These edits are pure comment hygiene — no logic change. Read paths already use the CASE derive (PR #33).

- [ ] **Step 1: Update the file-level comment around line 46-49**

Find the comment block that says something like "Used as fallback when express_sales.brand_kind is NULL — for example a code falls back to regex classification only when brand_kind is NULL." Rewrite to:

```python
# Used as fallback when the CASE derive in _BASE_QUERY produces NULL
# (resolved product has no brand_id). The regex classification is the
# last-resort heuristic when the product is unmapped or unbranded.
```

- [ ] **Step 2: Update the `_BASE_QUERY` comment at line 107-108**

Current:

```python
           -- Derive brand_kind from the resolved product's brand at read
           -- time, NOT from es.brand_kind. The cached column is set at
```

Rewrite to:

```python
           -- Derive brand_kind from the resolved product's brand at read
           -- time. (The express_sales.brand_kind cache was removed in
           -- mig 068; this is now the only source of truth.)
```

- [ ] **Step 3: Update the `get_invoice_line_breakdown` comment at line 635-636**

Current:

```python
               -- See _BASE_QUERY for rationale: derive from resolved
               -- product's brand, not the (possibly stale) es.brand_kind.
```

Rewrite to:

```python
               -- See _BASE_QUERY for rationale: derive from resolved
               -- product's brand (the brand_kind cache is gone per mig 068).
```

- [ ] **Step 4: Verify no other `brand_kind` cache references in this file**

```bash
grep -n "brand_kind" inventory_app/commission.py
```

Expected hits should now only be:
- `brand_kind` as a CASE alias / dict key (the derived value) — keep these.
- `classify_brand_kind` function (regex fallback) — keep.
- No mentions of `es.brand_kind`, `express_sales.brand_kind`, or "cache".

- [ ] **Step 5: Audit `_topup_pre_feb_for_product()` for residual cache logic**

```bash
grep -n -A 30 "def _topup_pre_feb_for_product" inventory_app/commission.py
```

If the function reads `es.brand_kind`, replace with the same CASE derive. If it only uses the derived Python-side value, leave alone. Per the issue: "its brand_kind-related logic if any remains after pass 4" — likely already clean, but verify.

- [ ] **Step 6: Run pytest to confirm comment changes broke nothing**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_commission_unit_aware.py tests/test_wacc.py -q 2>&1 | tail -10
```

Expected: pass (no behaviour change yet — cache still alive, tests use it).

- [ ] **Step 7: Commit**

```bash
git add inventory_app/commission.py
git commit -m "docs(commission): comment hygiene for post-cache world

Update three comment blocks in commission.py to remove stale references
to express_sales.brand_kind. The CASE derive in _BASE_QUERY and
get_invoice_line_breakdown is already the source of truth (PR #33);
these edits make the comments reflect that. No behaviour change."
```

---

## Task 7: Update `inventory_app/models.py:176` docstring

**Files:**
- Modify: `inventory_app/models.py`

- [ ] **Step 1: Read the current docstring at line 176**

```bash
sed -n '170,185p' inventory_app/models.py
```

- [ ] **Step 2: Rewrite the brand_kind reference**

Replace any mention of `express_sales.brand_kind` cache being refreshed by trigger with a one-line note that the cache was removed in mig 068 and brand_kind is now derived at read time in `commission._BASE_QUERY`.

- [ ] **Step 3: Commit**

```bash
git add inventory_app/models.py
git commit -m "docs(models): note brand_kind cache removal in mig 068"
```

---

## Task 8: Clean misleading template hint

**Files:**
- Modify: `inventory_app/templates/commission_invoice_detail.html`

- [ ] **Step 1: View the hint block at lines 117-120**

```bash
sed -n '115,125p' inventory_app/templates/commission_invoice_detail.html
```

Expected: a Thai-language hint suggesting users "refresh the brand_kind cache" (or delete + reimport sales) to fix stale rates.

- [ ] **Step 2: Rewrite the hint**

The cache is gone — there is no refresh path. If the badge shows a wrong `brand_kind`, the cause is now upstream (product not mapped, or brand_id not set on the product). Rewrite the hint in Thai to direct the user to:
- Check the product mapping in `/mapping`
- Check the product's `brand_id` in `/products/<id>`

Keep the styling. Replace ~3-4 lines of text only.

- [ ] **Step 3: Commit**

```bash
git add inventory_app/templates/commission_invoice_detail.html
git commit -m "ui(commission): replace stale brand_kind-cache hint

The cache is being removed in mig 068; the old hint suggesting users
'refresh brand_kind cache' or 'delete+reimport sales' no longer applies.
Replace with guidance to check product mapping and brand_id assignment
upstream — which is where the value now resolves from at read time."
```

---

## Task 9: Trim brand_kind cache-staleness regression tests

**Files:**
- Modify: `tests/test_commission_unit_aware.py`

- [ ] **Step 1: Inventory the brand_kind-related tests**

```bash
grep -n "def test_\|brand_kind" tests/test_commission_unit_aware.py | head -40
```

Identify which tests:
- Assert commission-amount correctness via the CASE derive (KEEP — they continue to work).
- Assert cache-staleness regression behaviour (REMOVE — the bug class is unreachable post-drop).

The spec calls out lines ~276-357 as the regression block. Verify by reading the docstring of each test in that range.

- [ ] **Step 2: Remove the cache-staleness regression block**

Delete the function(s) testing "stale es.brand_kind='own' must NOT pay own-brand rate after a product's brand_id was changed to point at a third-party brand." Approximate scope: lines 276-357. Verify by reading first — the block may include helper functions or fixtures that need to stay.

- [ ] **Step 3: Run the trimmed test file**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_commission_unit_aware.py -v
```

Expected: remaining tests pass (the kept ones don't depend on the cache).

- [ ] **Step 4: Commit**

```bash
git add tests/test_commission_unit_aware.py
git commit -m "test: remove brand_kind cache-staleness regression suite

The bug class (cached brand_kind drifts after products.brand_id update)
is unreachable post mig 068 — no cache to drift. The same correctness
property is enforced by the CASE derive in _BASE_QUERY, tested by
the remaining commission tests."
```

---

## Task 10: Trim cache assertion in `test_migration_061`

**Files:**
- Modify: `tests/test_migration_061_mapping_unit_aware.py`

- [ ] **Step 1: Find the assertion**

```bash
grep -n "brand_kind" tests/test_migration_061_mapping_unit_aware.py
```

Expected: one assertion around line 226 verifying that the rebuilt mapping refreshes `express_sales.brand_kind`.

- [ ] **Step 2: Remove the assertion, keep the surrounding test**

The mig 061 test itself (mapping rebuild correctness) is still valid; only the brand_kind cache assertion needs removal. Delete the lines that assert the cache value, keep everything else.

- [ ] **Step 3: Run the test file**

```bash
~/.virtualenvs/erp/bin/pytest tests/test_migration_061_mapping_unit_aware.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_migration_061_mapping_unit_aware.py
git commit -m "test(mig 061): drop brand_kind cache assertion

The mig 061 mapping-rebuild correctness test stays; the side-assertion
on express_sales.brand_kind is removed because the cache itself goes
away in mig 068."
```

---

## Task 11: Write mig 068 — drop trigger + index + column

**Files:**
- Create: `data/migrations/068_drop_express_sales_brand_kind.sql`
- Create: `data/migrations/068_drop_express_sales_brand_kind.rollback.sql`

- [ ] **Step 1: Confirm the trigger + index names (live DB audit)**

```bash
sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
  "SELECT name, type FROM sqlite_master WHERE tbl_name='express_sales' OR sql LIKE '%brand_kind%' ORDER BY type;"
```

Expected:
- `refresh_brand_kind_on_product_brand_change` (trigger)
- `idx_express_sales_brandkind` (index)

If a different trigger or index name appears, update the migration accordingly.

- [ ] **Step 2: Write the forward migration**

Create `data/migrations/068_drop_express_sales_brand_kind.sql`:

```sql
-- ============================================================================
-- Migration 068 — drop the express_sales.brand_kind write-only cache.
--
-- After PR #33, every commission read path derives brand_kind at read time
-- from brands.is_own_brand via a CASE expression in commission._BASE_QUERY
-- and commission.get_invoice_line_breakdown. The cache column is no longer
-- read by any production code path. This migration removes the column, the
-- trigger that kept it fresh, and the supporting index.
--
-- Pre-requisites already shipped on this branch:
--   - scripts/import_express.py no longer writes brand_kind on INSERT.
--   - scripts/load_brand_map.py no longer recomputes brand_kind.
--   - scripts/backfill_express_unit_normalize.py no longer recomputes.
--   - scripts/isolate_issue30_impact.py removed (issue #30 closed).
--   - inventory_app/commission.py + models.py + templates updated.
--   - Cache-staleness regression tests removed.
--
-- Forward-only. The rollback restores the column + trigger DDL but
-- cannot repopulate the cache; restoring is rarely useful given PR #33
-- already proved the cache redundant.
-- ============================================================================

BEGIN;

DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;
DROP INDEX   IF EXISTS idx_express_sales_brandkind;

ALTER TABLE express_sales DROP COLUMN brand_kind;

COMMIT;
```

- [ ] **Step 3: Write a minimal rollback**

Create `data/migrations/068_drop_express_sales_brand_kind.rollback.sql`:

```sql
-- ============================================================================
-- Rollback for mig 068. Restores the brand_kind column + index + a no-op
-- trigger placeholder.  Does NOT repopulate cached values — any consumer
-- that depends on them must be re-introduced AND backfilled manually.
-- ============================================================================

BEGIN;

ALTER TABLE express_sales ADD COLUMN brand_kind TEXT
    CHECK(brand_kind IS NULL OR brand_kind IN ('own', 'third_party'));

CREATE INDEX IF NOT EXISTS idx_express_sales_brandkind
    ON express_sales(brand_kind);

-- The unit-aware refresh trigger is non-trivial to restore here verbatim;
-- if rolling back, copy the trigger DDL from
-- data/migrations/063_brand_kind_unit_aware_trigger.sql and apply
-- separately.  This rollback ALSO leaves the cache empty — backfill
-- separately by running scripts/backfill_express_unit_normalize.py
-- (pre-mig-068 version).

COMMIT;
```

- [ ] **Step 4: Smoke-apply the migration against a tmp DB**

```bash
cp /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db /tmp/mig068_test.db
sqlite3 /tmp/mig068_test.db < data/migrations/068_drop_express_sales_brand_kind.sql
# Verify the column is gone
sqlite3 /tmp/mig068_test.db "PRAGMA table_info(express_sales);" | grep -c "brand_kind"
# Verify the trigger is gone
sqlite3 /tmp/mig068_test.db "SELECT name FROM sqlite_master WHERE name='refresh_brand_kind_on_product_brand_change';"
# Verify row count unchanged
sqlite3 /tmp/mig068_test.db "SELECT COUNT(*) FROM express_sales;"
```

Expected:
- `brand_kind` count = `0`
- Trigger query returns empty
- Row count matches the live DB row count.

- [ ] **Step 5: Commit**

```bash
git add data/migrations/068_drop_express_sales_brand_kind.sql data/migrations/068_drop_express_sales_brand_kind.rollback.sql
git commit -m "feat(mig 068): drop express_sales.brand_kind write-only cache

DROP TRIGGER refresh_brand_kind_on_product_brand_change;
DROP INDEX   idx_express_sales_brandkind;
ALTER TABLE express_sales DROP COLUMN brand_kind;

Forward-only. After PR #33, every commission read path derives brand_kind
at read time from brands.is_own_brand via _BASE_QUERY's CASE expression.
The cache had no consumers but was still maintained by trigger + write
sites. This migration ends the bug class (every future remap-style
operation becomes incapable of drifting commission rates because there
is no cached value to drift). Closes #34."
```

---

## Task 12: Run full pytest suite

**Files:** none — verification only.

- [ ] **Step 1: Run the full suite**

```bash
cd /Users/putty/Sendai-Boonsawat/sendy_erp
~/.virtualenvs/erp/bin/pytest -x -ra 2>&1 | tail -25
```

Expected: full suite passes. The trimmed test count should be ~395-405 (down from ~412 after the cache-staleness regression suite was removed). If any unexpected test fails, investigate:
- If a previously-passing test now fails because it indirectly relied on the cache, that's a missed consumer — find and convert to derive-at-read-time.
- If a test fails for unrelated reasons (e.g., depends on the dev server being down), note it but proceed.

- [ ] **Step 2: Commit any fix follow-ups, if any**

If a missed consumer surfaced, fix it with a separate small commit referencing this task. Don't bundle into mig 068's commit.

---

## Task 13: Post-drop equivalence validation

**Files:** none — produces `/tmp/commission_post.json` and a diff vs the Task 1 baseline.

- [ ] **Step 1: Apply mig 068 to a fresh tmp DB**

```bash
cp /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db /tmp/commission_post.db
sqlite3 /tmp/commission_post.db < data/migrations/068_drop_express_sales_brand_kind.sql
```

- [ ] **Step 2: Re-run the same capture script against the post-drop DB**

Copy `/tmp/capture_commission_baseline.py` to `/tmp/capture_commission_post.py` and change:
- `TMP_DB = "/tmp/commission_post.db"` (don't re-copy from live)
- `OUT = "/tmp/commission_post.json"`
- Remove the `shutil.copy2(LIVE_DB, TMP_DB)` block (and the WAL/SHM lines) so we use the existing post-drop DB.

Run:

```bash
~/.virtualenvs/erp/bin/python /tmp/capture_commission_post.py
```

- [ ] **Step 3: Diff baseline vs post**

```bash
diff -u /tmp/commission_baseline.json /tmp/commission_post.json | head -50
```

Expected: **NO output** (files identical) OR only formatting/order differences that are semantically equivalent.

If the diff shows numeric differences in any commission amount, STOP. Either (a) PR #33's CASE derive isn't actually equivalent to the cache (which would mean PR #33 was wrong — unlikely but check), or (b) a writer was missed AND the un-removed cache was being read somewhere we missed.

Investigate by:
- Running `rg "es\.brand_kind|express_sales\.brand_kind" inventory_app/` to find missed readers.
- Running `rg "brand_kind" inventory_app/` and inspecting any non-CASE-derive references.

- [ ] **Step 4: Document the result**

If diff is empty:

```bash
echo "Equivalence gate passed: $(wc -l < /tmp/commission_baseline.json) lines, byte-identical." \
  > /tmp/equivalence_result.txt
cat /tmp/equivalence_result.txt
```

If not empty, document the divergence in a comment on issue #34 (and stop the PR until resolved).

---

## Task 14: `/scrutinize` + `/codex:adversarial-review` gates

**Files:** none — review only.

- [ ] **Step 1: Self-scrutinize via the skill**

Invoke `/scrutinize` on the branch diff. Expected focus areas (the reviewer should call these out independently):
- Was anything missed in the consumer sweep?
- Are the test removals strictly losing coverage of the cache, or accidentally losing other coverage?
- Is the migration safely idempotent if re-run? (Mig runner is filename-keyed; should be safe.)
- Is the rollback truly useful or honestly documented as imperfect?

- [ ] **Step 2: Fix any blocking findings**

Apply suggested changes inline (or push back if the finding is wrong, with rationale).

- [ ] **Step 3: Codex adversarial pass**

Run `/codex:adversarial-review` after `/scrutinize` is clean. Same expectation as PR #42: catch any "hidden cache update path" findings.

- [ ] **Step 4: Fix any new blockers**

Same as Step 2.

---

## Task 15: Push branch + open PR closing #34

**Files:** none — workflow only.

- [ ] **Step 1: Pre-push sanity**

```bash
git fetch origin && git log --oneline origin/main..HEAD | head -20
~/.virtualenvs/erp/bin/pytest -x -ra 2>&1 | tail -3
```

- [ ] **Step 2: Push the branch**

```bash
git push -u origin fix/drop-express-brand-kind-cache
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --title "fix(express): drop write-only brand_kind cache (closes #34)" --body "$(cat <<'EOF'
## Summary

End the write-only `express_sales.brand_kind` cache as a class of bug.
After PR #33, commission read paths derive `brand_kind` at read time
from `brands.is_own_brand` via a CASE expression. The cache column
remained, doing maintenance work for no consumer. This PR removes it.

## Spec & plan

- Spec: `docs/superpowers/specs/2026-05-21-drop-express-brand-kind-design.md`
- Plan: `docs/superpowers/plans/2026-05-21-drop-express-brand-kind.md`

## What changed

- **Migration 068** — `DROP TRIGGER`, `DROP INDEX`, `ALTER TABLE DROP COLUMN`.
- **Writer removal** — `scripts/import_express.py`, `load_brand_map.py`,
  `backfill_express_unit_normalize.py`. Each stripped of `brand_kind`
  recompute logic; unrelated logic preserved.
- **Dev-script deletion** — `scripts/isolate_issue30_impact.py` (issue #30 closed).
- **Comment hygiene** — `commission.py`, `models.py`, `templates/commission_invoice_detail.html`.
- **Test trim** — removed cache-staleness regression suite from
  `test_commission_unit_aware.py` + the side-assertion in `test_migration_061`.

## Validation gate

`get_commission_for_month` results for 2026-03, 2026-04, 2026-05
pre-drop and post-drop are byte-identical (documented in
`/tmp/equivalence_result.txt`).

## Test plan

- [ ] CI: `pytest -x -ra` → expect ~395-405 pass
- [ ] Codex review pass after PR opens
- [ ] Post-merge: run mig 068 on Railway; back up DB first via
      `scripts/backup_db.sh`. Validate one commission month with real
      data before announcing.

## Risks + mitigations

- **SQLite version on Railway** — local 3.51.0 ✅. Migration fails
  loudly if Railway has < 3.35.0; revert + bump Nixpacks if so.
- **Money-path table (18,599 rows)** — `ALTER TABLE DROP COLUMN`
  rewrites the table; backup before deploy.
- **Forward-only migration** — rollback restores DDL but not cached
  values; the cache is provably redundant post-PR #33.

Closes #34

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify PR URL**

```bash
gh pr view --json url --jq .url
```

Expected: GitHub URL printed.

---

## Definition of done

- Mig 068 applied cleanly to live-DB clone, column gone, trigger gone, index gone.
- Full pytest green (~395-405 tests).
- Equivalence gate passed: byte-identical commission output for 3 months.
- `/scrutinize` + Codex adversarial review pass with no unresolved blockers.
- PR opened, ready for Put's squash-merge.
- Issue #34 referenced in PR title + body so it closes on merge.
- Post-deploy: Put validates one real commission month on Railway before announcing.
