# Drop `express_sales.brand_kind` Cache — Design

**Date:** 2026-05-21
**Status:** Approved (issue #34 body serves as ratified scope)
**Origin:** [GitHub issue #34](https://github.com/Zyringe/sendy-erp/issues/34) — strategic refactor after PR #33 (commit `98442df`) made `brand_kind` a write-only cache.

## Context

After PR #33 (Codex pass 8), commission read paths derive `brand_kind` at read time from `brands.is_own_brand` via a CASE expression:

- `inventory_app/commission.py:107-117` — `_BASE_QUERY` (used by `get_commission_for_month`, `get_commission_for_salesperson_month`).
- `inventory_app/commission.py:632-639` — `get_invoice_line_breakdown` (per-invoice view).

`express_sales.brand_kind` is still WRITTEN by:
- `scripts/import_express.py:92-330` — `_brand_kind_for_product()` + the INSERT.
- `scripts/load_brand_map.py` — recompute step.
- `scripts/backfill_express_unit_normalize.py` — recompute step.
- `data/migrations/021_brand_kind_trigger.sql` — original trigger (BEFORE INSERT).
- `data/migrations/063_brand_kind_unit_aware_trigger.sql` — unit-aware refresh trigger on `products.brand_id` UPDATE + a one-time recompute side-effect (already executed in prod).

Plus ONE remaining live reader the issue body doesn't list:
- `scripts/isolate_issue30_impact.py:54` — `SELECT es.brand_kind AS brand_kind`. This is a dev-time diagnostics script for the (now-closed) issue #30 impact investigation — not on any production path. Treat as code-to-delete-or-derive, not as an active blocker.

## Goal

End the `brand_kind` cache as a class of bug. After this PR, no maintained code path reads OR writes the cache; the column is gone from the schema; the triggers that kept it fresh are dropped. Every future remap-style script becomes incapable of drifting commission rates because there is no cached value to drift.

## Non-Goals

- **No refactor of `_BASE_QUERY` or any read path.** PR #33 already shipped the read-time derive. This PR only removes the now-dead cache.
- **No change to `classify_brand_kind()` regex fallback.** Still used when a resolved product has no `brand_id` (NULL fallthrough). The CASE returns NULL in that case and the Python code falls back to `classify_brand_kind(product_name_raw)` — same behaviour as today.
- **No change to BSN sync / `sales_transactions`.** Different table, different code path, separate cache (if any).
- **No deprecation period or two-phase rollout.** Single PR, single mig. Equivalence is enforced by a hard validation gate before merge (see Validation).

## Scope

### Consumer surface (verified by `grep -rn "brand_kind"` sweep)

**Schema (mig 068 must handle):**
- `data/migrations/021_brand_kind_trigger.sql` — adds `brand_kind` column + the original trigger.
- `data/migrations/063_brand_kind_unit_aware_trigger.sql` — replaces the mig 021 trigger with the unit-aware version + one-time recompute (already applied).

The column itself was added in mig 021. Triggers are layered (063 replaces 021's). Both triggers must be dropped before `ALTER TABLE DROP COLUMN`. Trigger names (verify from live DB):
- `refresh_brand_kind_on_product_brand_change` (mig 063 — the unit-aware one).
- Any mig 021-era trigger that might still exist (mig 063 may have done `DROP TRIGGER IF EXISTS` already; verify).

**Production code to clean (writers):**
- `scripts/import_express.py:92` — delete `_brand_kind_for_product()`.
- `scripts/import_express.py:304, 309, 320` — strip `brand_kind` from the INSERT INTO express_sales statement (column list AND values).
- `scripts/load_brand_map.py` — strip the brand_kind recompute step; keep the brand-mapping load if useful.
- `scripts/backfill_express_unit_normalize.py` — strip the recompute step; keep the alias-seeding + unit-normalization logic which is still useful.

**Production code to clean (read side):**
- `inventory_app/commission.py:46-49` — comment ("Used as fallback when express_sales.brand_kind is NULL") becomes obsolete; rewrite or delete.
- `inventory_app/commission.py:107-108` — comment ("Derive brand_kind from the resolved product's brand at read time, NOT from es.brand_kind") — keep the intent, drop the "NOT from es.brand_kind" clause (no longer a comparison).
- `inventory_app/commission.py:635-636` — same comment cleanup in `get_invoice_line_breakdown`.
- `inventory_app/commission.py` `_topup_pre_feb_for_product()` — audit for any residual brand_kind logic.
- `inventory_app/models.py:176` — docstring reference; rewrite.
- `inventory_app/templates/commission_invoice_detail.html:78-81` — `ln.brand_kind` reads from the Python dict (set by the CASE derive), still works — leave alone.
- `inventory_app/templates/commission_invoice_detail.html:117-120` — misleading hint text "refresh brand_kind cache" — rewrite to reflect the new world (no cache).

**Diagnostic / dev scripts:**
- `scripts/isolate_issue30_impact.py:54` — kill the `es.brand_kind` SELECT. Easiest: delete the script entirely (issue #30 closed); else rewrite to derive at read time.

### Migration 068 — single forward-only migration

```sql
-- data/migrations/068_drop_express_sales_brand_kind.sql
BEGIN;

-- Drop the triggers that kept the cache fresh.
DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;
-- The mig 021 trigger may have been replaced by mig 063 with the same name,
-- but include any historical trigger name we find at audit time. The PR
-- implementation will run a `SELECT name FROM sqlite_master WHERE
-- type='trigger' AND tbl_name='express_sales'` audit first to enumerate.

-- Drop the column.
ALTER TABLE express_sales DROP COLUMN brand_kind;

COMMIT;
```

**SQLite version:** local dev is 3.51.0 (well above the 3.35.0 cutoff for `ALTER TABLE DROP COLUMN`). Railway runs Python 3.9 via Nixpacks → Debian-based base image → SQLite ≥ 3.40 in practice. The migration will fail loudly at deploy if the runtime SQLite is too old; that's an acceptable smoke failure (revert + bump Nixpacks base).

**Rollback:** forward-only. The `.rollback.sql` would need to re-add the column AND re-populate it from `brands.is_own_brand`. Given the cache is now provably write-only with PR #33, rollback means restoring a dead cache, which is non-useful. Document this in the migration header.

### Tests to remove / update

The issue body lists two by name. Full sweep:

**Remove (test the cache contract, which is going away):**
- `tests/test_commission_unit_aware.py:276-357` — the entire "stale es.brand_kind='own' must NOT pay own-brand rate" stretch (issue body's mention). This is the regression guard for the cache-staleness bug; after column drop, the bug is unreachable.
- `tests/test_migration_061_mapping_unit_aware.py:226+` — case asserting `brand_kind` refresh through the rebuilt mapping. The mig itself stays; the cache assertion goes.

**Audit (may need updating, not removing):**
- The 5 other test_commission_unit_aware.py references — some test commission *correctness* (which still holds via the CASE derive); some test cache *behaviour* (which goes away). Triage line-by-line during implementation.

**Net expected impact:** ~412 - 5-15 tests = ~395-405 tests after the trim. Equivalence is preserved (the CASE derive already passes the surviving tests today).

## Validation gate (hard requirement before merge)

The issue's acceptance criterion: *"Live DB validation: `get_commission_for_month` results for the most recent 3 months produce identical numbers pre-drop and post-drop (use a fresh tmp-DB copy)."*

Implementation:

1. **Baseline capture (BEFORE any code change):**
   - Copy `inventory_app/instance/inventory.db` → `/tmp/commission_baseline.db`
   - Run `python -c "from commission import get_commission_for_month; for ym in ('2026-03', '2026-04', '2026-05'): print(ym, get_commission_for_month(ym, ...))"`
   - Save full output as `/tmp/commission_baseline.json`.

2. **Post-drop replay (AFTER all changes + mig 068 applied):**
   - Copy the live DB again to `/tmp/commission_post.db`
   - Run mig 068 against the copy.
   - Run the same commission function across the same 3 months.
   - Diff against `commission_baseline.json` — must be byte-identical (after JSON normalization).

3. **If diff is non-empty:** STOP. Either (a) the CASE derive isn't equivalent to the cache (which would mean PR #33 was wrong — unlikely given it shipped reviewed and validated), or (b) we missed a writer/reader. Investigate before proceeding.

## Risks

| Risk | Mitigation |
|---|---|
| **SQLite < 3.35.0 on Railway** | Migration fails at deploy; revert + bump Nixpacks. Pre-check by running `sqlite3 --version` in a Railway shell before merge if Put wants belt-and-suspenders. |
| **Missed reader** | Pre-implementation `grep -rn "brand_kind"` sweep finds them all; Codex adversarial review pass catches anything regex missed. The validation gate proves equivalence even if a reader is missed (because it would produce a different value). |
| **Trigger name ambiguity** | Implementation runs `SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='express_sales'` before writing the migration's `DROP TRIGGER` statements. |
| **Rollback need** | Forward-only by design (the cache is provably dead post-PR-#33). If something goes wrong post-deploy, revert the migration manually + restore from `data/backups/` snapshot. |
| **Money-path table (18,599 rows)** | `ALTER TABLE DROP COLUMN` in SQLite ≥ 3.35.0 rewrites the table. Operation is atomic per-statement, no concurrent-write risk in single-tenant prod. Backup before deploy as standard practice (`scripts/backup_db.sh`). |

## Acceptance criteria (from issue #34, with implementation handoff)

- [ ] `rg "express_sales\.brand_kind|es\.brand_kind"` returns zero hits in `inventory_app/`, `scripts/` (except deleted ones), `tests/`, `data/migrations/` post-068, `templates/`.
- [ ] Full pytest passes (target: ~395-405 tests after cache-contract removals).
- [ ] Codex adversarial-review pass on the cleanup PR returns no findings related to "missing cache update path" or "stale cache" semantics.
- [ ] Equivalence gate above: byte-identical `get_commission_for_month` output for last 3 months pre vs post drop.
- [ ] Railway deploy validated on a recent commission month with real data before announcing.

## Out of scope (explicit)

- Renaming any column or refactoring `_BASE_QUERY`.
- Anything in the BSN sync path or `sales_transactions`.
- Touching the regex `classify_brand_kind()` fallback (still used when a resolved product has no `brand_id`).
- A deprecation period (the cache is already dead-read after PR #33).
