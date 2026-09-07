# Hammer pack/loose bundle — Phase 1 + Phase 2 — review request

Date: 2026-08-15 · Two open PRs, **neither merged** · Reviewer: Codex (round 3 of this arc)
Plan of record: `docs/plans/2026-08-14-hammer-pack-bundle-plan.md` (already reviewed by you in rounds 1 and 2; all rulings applied)

| | |
|---|---|
| **PR #387** | Phase 1 — `scripts/hammer_bundle_datafix.py` + tests. **Adds files only**, changes no data. |
| **PR #388** | Phase 2 — pair form learns a packaging component; `cross_unit_hazard` learns `role`. |
| Already shipped | **#385** (mig 158: `conversion_formula_inputs.role`, `ux_conv_active_pack_per_output`, `models/conversion_roles.py`) — merged, prod-verified |
| Not done, deliberately | **W4** — the +18 card ADJUST and the two conversion runs. Human, one-submit-at-a-time, in the UI. |

## Where the previous rounds landed (context you gave, now implemented)

- **Row order cannot carry business meaning** → `role` column (mig 158, shipped). Both PRs read the component by role, never by position, and both prove it with reverse-order tests.
- **`run_conversion` commits stock before costing** → not touched here (shared money path, 122 formulas). Scoped instead to a **read-only WACC-identity preflight before W4** — which I have now run on prod: all five products (268/269/270/271/869) pass. It **reduces** the known risk; it does not close the partial-success window, so the W4 runbook's stop-and-verify-after-every-submit stands.
- **Validator scope is an explicit invariant**, not `len(inputs) > 1`: active `[แพ็ค]` only.
- **"byte-identical rollback"** → logically identical schema + data.

## PR #387 — Phase 1 data-fix script

Three checkpoints: **W1** rename (268 `model='#BSN01'`; 269 `model='#BSN02'` + `series='ตารางกันลื่น FN'`; 271 `series` underscore→space) through `naming_cascade.save_product`, so `sku_code` is never touched. **W2** `opening_cost` 869=5 / 268=71 / 269=73 then WACC replay. **W3** the two `[แพ็ค]` bundle formulas with `role`.

Guard shape: baseline gate (OLD → apply, NEW → skip, neither → refuse and exit non-zero) · `BEGIN IMMEDIATE` → mutate → assert → **rollback on failed postcondition** → commit → re-read on a fresh connection · idempotent and resumable · `rehearse` refuses `/data/inventory.db` and the shared dev DB outright.

**Verification: 24 tests, break-it-once on all five guards** (W2 rollback, W3 rollback, W1 baseline gate, live `--confirm`, rehearse protected-path) — each confirmed red for its specific reason, then restored.

**Rehearsed on real prod data** (114MB `.backup` inside the container, verified by a separate process on a fresh connection, snapshot deleted): names and costs exactly as designed, `sku_code` unchanged on all four products, 271's rendered name unchanged, formulas `(270,component)+(869,packaging)` / `(271,component)+(869,packaging)`, **zero `[แกะ]`**, `stock_levels` untouched, second run 8/8 "skipped".

### Two things I changed after the agent handed it over
1. **`rehearse` had no production-path guard** — it differed from `live` only in not requiring `--confirm`, so `--mode rehearse --db-path /data/inventory.db` would have mutated prod with nothing asking. Added `is_protected_db_path()` + 4 tests + break-it-once.
2. Nothing else; the constants were checked line-by-line against a prod read I took independently before the agent reported.

## PR #388 — Phase 2 pair form + unit guard

- `upsert_pack_unpack_pair(packaging_id=...)`: 2-input `[แพ็ค]` with roles, **no `[แกะ]` half** (a blister card is destroyed on opening), validated through `conversion_roles` before any write. Omitting the kwarg is byte-identical to today.
- **Dedup change**: the `[แพ็ค]` spec now dedups on `(output, name LIKE '[แพ็ค]%')` instead of `(output, frozenset(inputs))`, because mig 158's unique index permits only one active `[แพ็ค]` per output — so switching a plain pair into a bundle must be an UPDATE, not a second INSERT. The `[แกะ]` spec **keeps** the input-set key, because a loose product legitimately unpacks from several packs.
- `derive_pair_from_formula` reads the component by role, so the list's pencil button reopens a bundle prefilled.
- `cross_unit_hazard`: single-input path unchanged; multi-input `[แพ็ค]` resolves the partner by role; **fails closed (raises) on a role-less or ambiguous multi-input** rather than guessing. "Any input whose unit matches the BSN unit" was rejected — two components can share a unit.
- Route + template: optional picker, direction forced to pack-only server-side (not just in JS), self-reference guard, `ConversionRoleError`/`IntegrityError` → flash + reshow, never a 500.

**Verification: 19 new tests**, break-it-once on all five guards — and two of them were honestly reported as having stayed **green** on the first attempt (the agent's own bundle builder always inserted loose-before-packaging, so `ins[0]` coincidentally equalled the component); dedicated reverse-insertion-order tests were added that do go red. A sixth issue was self-caught against the new "a 302 is not evidence the write happened" rule: a route test asserting `302 + count unchanged` was replaced with an assertion on the written roles, and the mutation proof was done with a stubbed upsert that still 302s.

**Real booted server** (not just test client): booted against a throwaway copy on port 5055 with CSRF on, logged in for real, POSTed a bundle → 302 → verified in the DB that formula rows and roles landed, reopened the edit prefilled, and confirmed the self-reference POST reshows at 200 instead of 500.

## Suite parity

Measured like-for-like — same commit base, same DB state (this matters: my first attempt compared 10F/22E against 11F/39E and the entire difference was **population**, one worktree's DB being at mig 157 and the other at 158, not a regression).

- Baseline (`origin/main` @ `baa6041`, own DB at mig 158): `10 failed, 3613 passed, 54 skipped, 22 errors in 597s`
- PR #388 branch, rebased onto the same commit: `10 failed, 3632 passed, 54 skipped, 22 errors in 596s`
- Failure NAME-set diff: **32 names each, byte-identical** (`diff` empty). The `+19 passed` is exactly the 19 new tests.

The 10F/22E that both sides carry are pre-existing: the errors are the known drop-first-fixture
migration-test debt (`test_mig150_audit_log_row_key`, `test_vat_book_view`) tracked in TASKS.md.

Phase 1 (#387) adds files under `scripts/` and `tests/` only and is imported by no app code, so its suite effect is its own test file (24 passed).

## Environment facts worth knowing (both cost me time today)

1. **Each `railway ssh` invocation gets a fresh ephemeral filesystem.** Files written to `/tmp` or `/app` are gone by the next invocation; only the `/data` volume persists. A verification step in a *second* ssh call reads an empty file that `sqlite3.connect` helpfully creates — which looks exactly like "the table vanished". Any prod op must ship, run, verify and clean up in **one** invocation.
2. **Prod runs SQLite 3.46.1; this dev machine runs 3.51.0.** Mig 158's rollback uses `ALTER TABLE ... DROP COLUMN` on a column carrying a CHECK constraint, which the SQLite docs suggest is disallowed. It works in both — but only because I tested it *in the prod runtime*, not because the agent's local test passed.
3. The shared dev DB was still at mig 157 while prod was at 158. It self-heals on the next app boot; no action needed, but it silently changes what `tmp_db`/`empty_db` clone, which is what produced the false suite delta above.

---

# Round 5 follow-up — the W2 first-run blocker is closed

**PR #387, commit `736e09e`** (on top of the reviewed `ae8c5ca`). `39 passed` (was 38).

Your finding was right and was the sharpest of the arc: `_w2_ledger_replayed` had **exactly one call site**,
`_w2_state`, the rerun classifier. `run_w2` checked only `opening_cost` + `cost_price` before `COMMIT`, so a
replay setting `cost_price` to exactly 71/73/5 while writing no ledger committed on the **first** run — the
rerun would refuse, too late. Correct guard, wrong path.

- `_w2_ledger_replayed` now runs in the **pre-commit** postcondition (failure joins `problems` → existing
  `ROLLBACK`), and again on the **fresh post-commit** connection so the independent read proves the same
  contract, not a weaker one.
- **Mutation test** rather than a neutered guard: a stub that writes the exact target `cost_price` and no
  ledger rows must leave the DB fully at baseline — `opening_cost` and `cost_price` back at baseline for all
  three products and zero ledger rows, with a control first proving the ledger was empty to begin with.
- Break-it-once: neutering the pre-commit check makes that test fail `assert 71.0 == 76.0` on 268's
  `opening_cost` — it **committed instead of rolling back**. The test fails on the damage, not on a missing
  exception.
- Re-rehearsed on a fresh prod-data snapshot under the strictest gates: run 1 applies, run 2 skips 8/8,
  independent read confirms `opening_cost` = `cost_price` = 71/73/5, ledger `INITIAL` rows at 71/73/5, both
  formulas with `(component, packaging)` roles, `sku_code` unchanged. Snapshot deleted.

**Your #388 P2 is accepted and split out as issue #389.** The over-claim was in my PR comment, not the code —
`record_conversion_role_alert`'s own docstring already says best-effort — and that comment is corrected on the
PR. Stock/WACC correctness is unaffected: the hazard is still returned and every write caller still blocks.

**Ordering unchanged and being followed**: #387 → run W1–W3 → W4 (all three submits, runbook) → rebase and
re-run parity → merge #388. Nothing merged; prod data untouched.

---

# Round 4 follow-up — **both PRs, all findings from rounds 3 and 4 closed**

Read this section first; the round-3 section below is kept for history. Every finding you raised in both
rounds was re-verified against the code before being accepted — none was wrong, and two of them
(`_flash_unit_hazard`'s `else` branch, and the W2 fixture that was itself the bug pattern) turned up further
problems while being fixed.

## PR #387 — round-4 fixes (`ae8c5ca`, `c21ac3f`) · `38 passed` (was 32)

| your finding | resolution |
|---|---|
| **BLOCKER** W2 DONE did not prove the ledger replayed | `_w2_ledger_replayed`: DONE now requires an `INITIAL` row at the target unit cost/WACC **and** the ledger tail (ordered as `get_current_wacc` orders) at the target WACC. Columns right + ledger wrong ⇒ UNKNOWN ⇒ refuse. A pre-existing fixture that set the columns with no ledger — literally this bug pattern — now seeds through the real `recalculate_product_wacc`. |
| W1 DONE did not check `sku_code` | Required now, with its own test separate from the OLD-gate one. |
| W3 fresh re-read weaker than its DONE gate | Extracted to `_w3_fresh_matches`; exact name is now part of the contract both paths check. |
| W3 OLD accepted other active formulas | OLD now requires no active formula of **any** kind for 268/269 (the unique index only covers `[แพ็ค]%`, and `get_buildable` sums all). Output side only; 270/271/869 as inputs elsewhere stay unrestricted. |
| W4 runbook gaps | All three applied — see the plan doc's W4 section. |

**Re-rehearsed on a fresh prod-data snapshot, three consecutive runs in one container invocation**: run 1
applies, runs 2 and 3 report **8/8 skipped**. That was the check that mattered — the new ledger-proof DONE gate
accepts the ledger the script's own run produced rather than refusing itself. Independent read: `268 INITIAL
71/71`, `269 INITIAL 73/73`, `869 INITIAL 5/5`, both formulas `output_qty` 1 with `(component, packaging)`,
`sku_code` unchanged everywhere. Snapshot deleted.

## PR #388 — round-3 fixes (`80e4421`, `95041f7`, `9db1662`, `2b45440`) · suite parity clean

| your finding | resolution |
|---|---|
| **BLOCKER** reciprocal `[แกะ]` survived a bundle conversion | Owner ruled **auto-deactivate**. Found via `find_pair_partner` *before* the inputs are mutated, `is_active=0` in the same transaction, reported in the return value and the flash. Deactivated, never deleted. |
| dedup silently rewired a recipe to a different loose SKU | Refuses on component mismatch (and on an unreadable existing formula) telling the operator to delete/deactivate first. Packaging swaps, ratio/note edits and plain↔bundle of the **same** component stay in-place updates. |
| `cross_unit_hazard` raising 500s whole pages | Returns `{'kind':'configuration_error','formula_id','message'}`. Three write callers block via a shared `_UNCONDITIONAL_BLOCK_KINDS`; `approve_pending_suggestion` needed no change (allowlist shape) and is covered by a test rather than left implicit; read/list paths render a warning; a deduped `system_alerts` row is recorded. **Also fixed a bug this uncovered**: `blueprints/bsn.py::_flash_unit_hazard`'s `else: # pack_piece` branch would have been reached by the new kind and `KeyError`'d. |
| malformed hidden field 500s | All three `int()` parses moved inside the validation `try/except`. |

**Coverage sweep** registers all six call sites with a written reason each and fails on any new unregistered
one. **Break-it-once on every guard** — including one the agent reported as having stayed **green**: the first
static control passed when a single loop lost its `except`, because the other loop kept the substring; replaced
with a `count(...) == 2` assertion that does catch it. **Real booted server** exercised the conversion, the
refusal, the malformed field, and the malformed-formula pages end to end.

**Suite parity, like-for-like** (base `baa6041`, same DB state): baseline `10F / 3613P / 54S / 22E` vs #388
`10F / 3663P / 54S / 22E` — failure NAME sets **32 vs 32, identical**.

## What I would like from this pass
Whether #387 is now safe to run against prod, and whether #388 is safe to merge **after** the W1–W3 run and W4
(your recommended ordering, which we are following). Nothing has been merged and prod data is untouched.

---

# Round 3 follow-up (kept for history) — was a re-review request for PR #387 only

All six of your findings were re-verified against the code before being accepted; none was wrong. #388's
fixes are still in progress — **do not review #388 yet**. This section covers only what changed on #387.

**Commits to diff: `d3f0e30..afdbda2`** (the two new ones are `6a18faa` and `afdbda2`).

| your finding | what changed |
|---|---|
| W3 called a formula DONE on inputs alone | `_w3_state` now requires the complete contract — `name`, `output_qty`, `is_active`, role/input shape. A near-miss is `unknown` → refuse, never silently normalised. This is what closes the interleaving hole you raised in Q4. |
| W1/W2 OLD gates incomplete | W1's OLD branch now also requires the old `product_name` **and** the expected `sku_code`; W2's now also requires the old `cost_price`. Values encoded from a prod read I took independently, before the agent reported. 271 still keys on the `series` column — its rendered name is identical before and after, so a name-based check would be wrong there. |
| W2 tests deleted the history the checkpoint exists to survive | Prod-shaped tests added per product. The 269 one **reproduces the bug first**: with `opening_cost` still 0 the ledger comes out with no `INITIAL` row at all and `wacc_after` 0, discarding a real ฿90/unit conversion cost. Then it asserts the post-W2 state with the negative-stock freeze visible (`unit_cost` 90 recorded, `wacc_after` held at 73, `stock_after` −12). Ledger, WACC, stock and `cost_price` are separate assertions. |
| W4 runbook assumed too much safety | Rewritten (`6a18faa`). Your `set_stock_to` catch was right and the runbook was wrong: it now says fresh-read 869's stock, set to 18 **only if it reads 0**, otherwise stop and reconcile. Step 1 (ADJUST) verifies the `transactions` row + `stock_levels` and asserts **no** conversion-cost row; steps 2–3 verify by a **typed unique `reference_no`**, not "the newest row". Preflight is re-run immediately before W4. |

**Verification of the fixes**: `32 passed` (was 24). Break-it-once on every gate touched — each neutered gate
turned its specific test red with `DID NOT RAISE`, then restored. The prod-shaped assertions were proven
non-vacuous with a stub that sets `cost_price` without writing the ledger: `cost_price == 71` still passed
while the ledger assertion failed, which is the point. Re-rehearsed against a fresh **prod-data snapshot**
inside the container under the stricter gates: prod still classifies as OLD, all three checkpoints apply, a
second run reports 8/8 skipped, independent read confirms names / unchanged `sku_code` / costs 71-73-5 /
both formulas with `output_qty` 1 and `(component, packaging)` roles. Snapshot deleted.

**What I would like from this pass**: whether the tightened gates are now complete enough to run against
prod, and whether the W4 runbook still assumes safety it does not have. Everything else can wait for #388.

---

## What I would most like you to attack

1. **The `[แพ็ค]` dedup widening.** It now updates *the* active `[แพ็ค]` for an output even when the submitted loose product differs from the stored one. Given the unique index the alternative is an IntegrityError, but is silently re-pointing an existing formula at a different loose SKU the right behaviour, or should the form refuse and make the operator delete first?
2. **`cross_unit_hazard` now raises.** It is called from mapping/unit-conversion paths, including list-building ones. A single malformed multi-input `[แพ็ค]` therefore turns a whole page into a 500 instead of a bad ratio. Fail-closed was the explicit ruling — but is raising at *that* call site the right shape, or should it degrade to "treat as hazard, flag the formula" so one bad row cannot take out a page?
3. **W2 rewrites cost history** (268's opening valuation 76→71, 269 gains one). You approved the direction in round 2; now that you can see the script, is the reconstruction it performs the one you meant?
4. **Ordering risk between the two PRs.** #388's form can write bundles as soon as it merges; #387's script writes the two real ones. If #388 lands first and someone uses the form on 268 before the script runs, the script's W3 baseline gate sees a NEW-state formula and skips — which I believe is correct, but it is the one interleaving I have not exercised.
5. Anything in the W4 runbook that still assumes more safety than the preflight actually provides.
