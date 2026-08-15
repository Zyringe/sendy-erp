# Hammer แผง = อัน + การ์ด — rename + 2-input bundle conversion — Plan

Created 2026-08-14 · **Revised 2026-08-15 after Codex review rounds 1 and 2** · Status: **reviewed, approved to build; nothing written yet**
Author: Claude (main thread), after a `/grilling` pass with Put
Reviewer: Codex — round 1 "rework before executing" (§2), round 2 "fix-plan-then-proceed" (§9). Both applied.

---

## 1. What Put asked for

Two things, on the live ERP (prod = `https://sendy.sendaibyboonsawat.com`):

1. **Rename two products** so the name makes the แผง (blister-pack) ↔ อัน (loose piece) relation obvious:
   - pid 268 `ฆ้อนด้ามไฟเบอร์ Sendai (แผง)` is the แผง version of pid 270 `ฆ้อนด้ามไฟเบอร์ Sendai #BSN01`
   - pid 269 `ฆ้อนด้ามไฟเบอร์ตารางกันลื่น Sendai (แผง)` is the แผง version of pid 271 `ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02`
2. **Add a bundle conversion on `/conversions`**: `1 แผง = 1 อัน + 1 การ์ด`, i.e. 268 ⟵ 270 + 869, and 269 ⟵ 271 + 869.

Put originally wrote pid **860** for the third component; during grilling he corrected it to **869 แผงฆ้อนหงอน**
(the empty blister card). 860 is `พุกพลาสติกแบบกิโล #10 สีขาว` (plastic wall anchors, sold by the kg).

## 2. Status after review round 1

Codex accepted the business shape (869 / ฿5 / 71 / 73 / pack-only / repair the negatives) and rejected the
execution plan on three stock/WACC grounds. **All three were verified against the code and prod data and all
three hold** (evidence in §6). The plan below is the reworked version:

| Codex finding | Adjudication | Where it landed |
|---|---|---|
| Input **row order** cannot carry business meaning | **Accepted** — verified: no `ORDER BY` in 3 readers, and **no index at all** on the table | Phase 0: `role` column |
| `run_conversion` commits stock **before** WACC recalc → partial success | **Accepted, scoped** — pre-existing app-wide shape; gated for this run rather than redesigned here | Pre-W4 gate + issue spun out |
| W4 repairs the balance, not the chronology; W2 rewrites cost history | **Accepted as disclosure** — both are deliberate, now stated in the plan and in the ledger notes | §5 W2/W4, §8 |
| Verification asserts `cost_price` where it cannot fail | **Accepted** — W2 alone would satisfy it | §7 assertion split |
| Duplicate active `[แพ็ค]` per output must be DB-enforced | **Accepted** — verified 0 violations on prod today, so the index is creatable | Phase 0: partial unique index |
| Master upload can overwrite prod WACC catalogue-wide | **Accepted, out of scope** — real, pre-existing, wider than this task | Separate issue, §9 |
| W1–W3 needs a resumable, checkpointed script | **Accepted** | §5, §7 |
| `cross_unit_hazard` should key on `role`, not first-input or unit-match | **Accepted** — fail closed when role is absent on a multi-input formula | Phase 2 |

**Consequence Put should know:** accepting the `role` column means the "insert the two formulas today, build
the form later" split he chose now needs **one small schema PR to land first**. The order becomes
Phase 0 (migration) → Phase 1 (data fix) → W4 → Phase 2 (form). Nothing else about his 11 decisions changed.

---

## 3. Ground truth (measured, not remembered)

Prod figures read **2026-08-14 / 15** over `railway ssh` with `/opt/venv/bin/python` (no sqlite3 CLI on prod),
against `/data/inventory.db`. Local figures from `inventory_app/instance/inventory.db` (checkout on `main`).

### Products

| pid | name | unit | base_sell | cost_price | opening_cost | stock PROD | stock local |
|---|---|---|---|---|---|---|---|
| 268 | ฆ้อนด้ามไฟเบอร์ Sendai (แผง) | แผง | 190 | 76 | 76 | **−6** | +6 |
| 269 | ฆ้อนด้ามไฟเบอร์ตารางกันลื่น Sendai (แผง) | แผง | 205 | **0** | 0 | **−12** | 0 |
| 270 | ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 | อัน | 180 | 66 | 66 | 1,157 | 1,157 |
| 271 | ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 | อัน | 195 | 68 | 68 | 910 | 910 |
| 869 | แผงฆ้อนหงอน (the empty blister card) | แผง | 0 | **0** | **0** | **0** | 0 |

⚠ local and prod disagree on 268/269 stock. **Prod is the source of truth**; local is a stale snapshot. Any
rehearsal of stock/WACC behaviour must run on a **prod snapshot** — the negative-stock branch of the WACC walk
cannot be reproduced locally.

### Cost ledger (prod)

- 268: one `INITIAL`, 2026-03-03, `ยอดยกมา 6 แผง @ 76.00`, `wacc_after = 76` · 270: `INITIAL` @ 66 ·
  271: `INITIAL` @ 68 · 187 (`แผงลูกบิด`, the knob card) `INITIAL` @ 10
- **269: no cost-ledger rows at all** (`opening_cost` 0 ⇒ the walk never emits an `INITIAL`)
- **869: no cost-ledger rows and no purchase rows ever.** Its `transactions` are 5 `ADJUST` (+48) and 4 `OUT`
  (−48, real BSN sales of the card to วรสวัสดิ์ at ฿10/แผง). Net 0 — the team ADJUSTs cards in on demand.

### Conversion data (prod)

- 123 formulas: 122 active (60 `[แพ็ค]` + 62 `[แกะ]`, **all single-input**) + 1 inactive multi-input, fid 126,
  the `ชุดฝาครอบลูกบิด` combo marker read by `marketplace_match._combo_components`.
- **No formula references 268/269/270/271/869 today.**
- 6 outputs have 2 active formulas — all of them a loose product that several packs unpack into.
  **0 outputs have more than one active `[แพ็ค]`**, so that invariant is true today and enforceable.
- 234 active products carry `unit_type = 'แผง'`; only **3** "empty card" products exist
  (187 `แผงลูกบิด`, 537 `แผงพุก+น็อต`, 869 `แผงฆ้อนหงอน`).

### Other verified facts

- **`/alerts` already lists every active product with negative stock** (`models/stock.py:8`). On prod that list
  is currently **exactly 2 rows: 268 and 269**, so the "how does Put know to pack" loop already exists.
- `platform_skus` rows exist for **270 and 271 only** (Lazada + Shopee), not for 268/269.
- BSN mapping already unit-correct: `006ฆ5280→268`, `006ฆ5290→270`, `006ฆ5260-1→269`, `006ฆ5260→271`, `000ก4001→869`.
- `unit_conversions` holds only each product's own unit — no cross-unit row, as the add-loose-variant rule requires.
- Photo folders on disk are keyed by `sku_code` for 268/270/271 → **`sku_code` must not move.**
- 268's pack photo shows a `BSN 03 MN` ("with magnet") sticker vs 270's `BSN 01`. Put confirmed the pack holds
  the #BSN01 hammer and the photo is stale (271's folder even holds a pack photo under a loose SKU).
- Highest migration on `origin/main` = **157** → this work takes **158**.

---

## 4. Decisions (Put's, taken during grilling)

| # | Decision |
|---|---|
| 1 | Third component is **869 แผงฆ้อนหงอน**, not 860 |
| 2 | **Pack direction only** — opening a blister destroys the card, so no `[แกะ]` half |
| 3 | Ship the formulas first, then grow the existing pair form (no restore of the removed N-input builder) |
| 4 | **Repair the negative stock** by running the new formulas retroactively (268 ×6, 269 ×12) |
| 5 | Card cost = **฿5** (฿10 is its resale price) |
| 6 | Real card stock on hand is **0** → ADJUST +18 in, run, ends at 0 |
| 7 | Rename **only the แผง side**; 270/271 keep their names |
| 8 | The hammer in the pack is the same SKU as the loose one |
| 9 | **Seed `opening_cost`** so cost is right immediately: 268 → 71, 269 → 73 |
| 10 | Claude does the data fix on prod (+ mirror to local); **Put runs W4 himself in the UI** |
| 11 | Teach `bsn_sync.cross_unit_hazard` about bundles |

---

## 5. Revised execution plan

### Phase 0 — migration 158 + validation helper (PR, merge = deploy)

**Schema:**

```sql
ALTER TABLE conversion_formula_inputs ADD COLUMN role TEXT
  CHECK (role IS NULL OR role IN ('component','packaging'));

CREATE UNIQUE INDEX IF NOT EXISTS ux_conv_active_pack_per_output
  ON conversion_formulas(output_product_id)
  WHERE is_active = 1 AND name LIKE '[แพ็ค]%';
```

- `role` is **nullable** so the 122 existing single-input formulas are untouched and keep meaning
  "the sole input is the partner". No backfill — a backfill would rewrite 122 rows of a money-adjacent table
  for no functional gain.
- The unique index encodes the invariant that makes `get_buildable` correct: it sums over **every** active
  formula for an output (`models/conversions.py:118-128`), so two active pack formulas for one output would
  double-count "แปลงได้ตอนนี้". Verified creatable: 0 violations on prod today. `[` is not a metacharacter in
  SQLite `LIKE`, so the pattern matches literally.
- Drop-first shape (`DROP INDEX IF EXISTS` before `CREATE`) so the file is re-runnable during rehearsal, plus a
  `158_*.rollback.sql`, plus `scripts/dump_schema.py` regen (enforced by `test_fresh_db_build.py`).
- **Central validation helper**, used by every writer (script and form alike), not just the form. Its scope is
  stated as an explicit invariant, because both it and the partial unique index key off the same prefix:

  | | rule |
  |---|---|
  | **Applies to** | **active** formulas whose `name` starts with `[แพ็ค]` — and nothing else |
  | Single input | `role` may be `NULL` (legacy shape) or `'component'` |
  | Multi input | **exactly one** `'component'` **and exactly one** `'packaging'` (the shape this form version creates) |
  | Every other formula | not interpreted by this validator: `[แกะ]` halves, the inactive combo marker (fid 126), and any future N-input manufacturing recipe are out of its reach by construction, not by accident |
  | Namespace moves | a rename that moves a formula **into or out of** the `[แพ็ค]` namespace must go through the validator; the unique index is the backstop that catches a writer which does not |

  Deliberately **not** keyed on `len(inputs) > 1`: that would silently forbid any future active multi-component
  manufacturing formula, which is a policy this task has no business setting.
- Tests: the migration applies and its rollback restores a **logically identical** schema and dataset — compare
  the `sqlite_master.sql` text of the affected objects plus the affected rows, not the DB file bytes (SQLite
  gives no byte-for-byte guarantee); the unique index actually rejects a second active `[แพ็ค]` for one output
  (break-it-once); the CHECK rejects a bogus role; the validator accepts the legacy NULL shape, rejects a
  role-less bundle, and leaves `[แกะ]` and the fid-126 combo marker untouched.

### Phase 1 — the data fix (guarded, resumable script; prod then local)

One script, four checkpoints, each: assert expected **old** values → mutate in a transaction → assert
invariants → commit → verify on a **fresh connection**. It refuses to run if prod state does not match the
recorded baseline, and can resume from the last completed checkpoint.

**W1 — rename.** Through `naming_cascade.save_product` (the exact function behind `/naming`,
`blueprints/naming.py:291`): backs up first, `BEGIN IMMEDIATE` + invariant asserts, rebuilds `product_name`
from the structured columns, and **deliberately does not touch `sku_code`** (issue #383).

| pid | column change | resulting name |
|---|---|---|
| 268 | `model = '#BSN01'` | `ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง)` |
| 269 | `model = '#BSN02'`, `series = 'ตารางกันลื่น FN'` | `ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 (แผง)` |
| 271 | `series` `'ตารางกันลื่น_FN'` → `'ตารางกันลื่น FN'` | unchanged: `ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02` |

The 271 edit closes a **latent silent-rename trap**: its stored name has a space where `series` has an
underscore, so `name_builder.rebuild_product_name(271)` returns `…ตารางกันลื่น_FN…` — the next person who
saves 271 for any unrelated reason renames it. Verified by running the builder against the live rows:
268/269/270 match their stored names, 271 does not.

Name format (`scripts/build_name_from_columns.py::build`):
`[category+series] [Brand] [#model][-size] [color] [(packaging)] [(condition)] [pack_variant]`; a Thai-initial
`series` attaches to the category with no space, which is why `'ตารางกันลื่น FN'` renders correctly. Target
shape matches the one existing precedent in the DB, pid 17 `…(แผง)` ⟷ pid 2012 `…(ตัว)`.

**W2 — cost basis.** `products.opening_cost`: **869 = 5, 268 = 71 (66+5), 269 = 73 (68+5)**, then
`recalculate_product_wacc` for each.

`opening_cost` is the right column: `models/wacc.py:194` seeds the ledger's `INITIAL` row from it specifically
and writes `cost_price` as the walk's *output* (seeding from `cost_price` would re-blend on every recompute,
mig 111). There is **no UI** for it — it is only ever set at product creation — which is why this is a script.

⚠ **This rewrites cost history, not just today's cost.** The seed replays the whole ledger: 268's `INITIAL`
goes from `@ 76.00` to `@ 71.00` (≈฿30 of opening inventory value) and 269 gains a cost basis it never had.
Any reader that walks `product_cost_ledger` sees the past change. Put approved this as a correction of a wrong
opening valuation. Verification therefore diffs the **whole ledger and the historical margin** for the three
products before/after, not just `cost_price`.

**W3 — the two formulas** (after W1, because `conversion_formulas.name` stores the product name as a snapshot):

```
[แพ็ค] ฆ้อนด้ามไฟเบอร์ Sendai #BSN01 (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน
  output 268 ×1, is_active 1 · inputs (270, 1, role='component') + (869, 1, role='packaging')

[แพ็ค] ฆ้อนด้ามไฟเบอร์ตารางกันลื่น FN Sendai #BSN02 (แผง) ⟵ 1 อัน + แผงฆ้อนหงอน
  output 269 ×1, is_active 1 · inputs (271, 1, role='component') + (869, 1, role='packaging')
```

**`is_active = 1` is mandatory, not cosmetic.** `marketplace_match._combo_components`
(`marketplace_match.py:241`) reads formulas with **≥2 inputs AND `is_active = 0`** and treats them as
marketplace combo packs, substituting the pack's product id with its components when matching orders to
Express IVs. An inactive 2-input formula here would silently join a money-adjacent matcher.

**W4 — stock repair (Put, in the prod UI, after a preflight gate)**

*Gate first (Claude, read-only):* run the WACC identity preflight for all five products (268, 269, 270, 271,
869) and confirm none raises. Reason: `run_conversion` **commits the stock movement and the cost log, then
recalculates WACC** (`models/conversions.py:485` onward). A failure in that recalc leaves the inputs consumed
and the output posted while the UI shows an exception — and because a page reload mints a fresh `run_token`,
an operator who reads that as failure can convert a second time. The replay guard does not protect across a
reload. The preflight **substantially reduces the known WACC-identity risk; it does not close the
partial-success window**, because the post-commit recalc can still fail on something the preflight cannot see.
Independent post-run verification is therefore mandatory, not optional.

*Then Put, one submit at a time:*

**Step 0 — immediately before starting**, not "earlier today": re-run the WACC identity preflight for all
five products. A preflight from hours ago is stale — imports and ledger writes happen in between.

**Step 1 — card stock.** ⚠ The ปรับ flow on `/products/869` is **"set stock to N", not "add N"**
(`models/transactions.py::set_stock_to` is absolute by design: a diff computed from an earlier read lands on
N ± whatever moved, and prod runs two workers). So fresh-read 869's stock first:
- reads **0** → set it to **18**, note `การ์ดที่ใช้แพ็คย้อนหลัง (ไม่มีบิลซื้อ) — ต้นทุน ฿5 ตามที่เจ้าของกำหนด`
- reads **anything else** → **STOP** and reconcile whether that is real card stock or drift. Do not reuse the
  number 18 from this document; it was derived when the stock read 0.

**Step 2** — run the 268 formula ×**6**. **Step 3** — run the 269 formula ×**12**. Type an explicit unique
`reference_no` per run (e.g. `W4-268-<date>`) so the ledger can be queried by that exact reference instead of
by "the newest row".

**Runbook — applies to every submit, whether the screen says success or error:**

- **Stop after each submit.** Do not reload, do not retry, do not start the next step.
- **Before** each conversion run, fresh-read: the formula's id / name / output / input roles and quantities,
  the input stocks, the input WACCs (expect 66 / 68 / 5), the target pack cost (71 / 73), and that no earlier
  W4 run already used that reference.
- **After** each submit, Claude fresh-reads on a new connection — and what to read differs by step:
  - **Step 1 (ADJUST)** writes **no** `conversion_cost_log` row. Verify the `transactions` row (exact note and
    delta) plus `stock_levels`, and assert that **no** conversion-cost row appeared.
  - **Steps 2–3 (conversions)** verify `conversion_cost_log` **by the typed reference_no**, the `transactions`
    rows carrying that reference, and `stock_levels` for all three products involved.
- A run counts as **committed** if those rows exist — even if the browser showed an exception. In that case
  the correct next action is to repair costing, never to re-submit.
- Only once the read confirms the expected before → after values does the next submit happen, and the expected
  values for the next run come from the **verified** state after the previous one, not from this document.
- If a run is committed but costing failed, stop the whole sequence and re-derive from the ledger before
  touching anything else.

⚠ An `ADJUST` is a non-purchase IN: it moves quantity and creates **no cost event**. The card's ฿5 comes
entirely from the synthetic `INITIAL` seed in W2, so the audit trail reads "no purchase bill, cost held at ฿5
by owner's estimate". The note text above is what makes that legible later.

Expected end state: 268 = 0, 269 = 0, 869 = 0, 270 = 1,151, 271 = 898, `/alerts` negative list empty.

**What W4 does *not* do:** it repairs the **current balance only**. The 18 packs were sold Feb–May 2026; the
repair posts today's ledger date, so the hammers leave stock in August. Any stock-as-of report for Feb–Jul
remains wrong. Backdating was considered and rejected: `run_conversion` has no event-date parameter and stamps
`date('now')` on the cost log (`models/conversions.py:451`), the WACC walk orders by `created_at` while
matching conversion costs by `reference_no`, and nobody knows which day each pack was actually assembled — a
chosen date would be fake precision. See the ledger-head hazard from PR #355.

### Phase 2 — extend the pair form (PR, after W4)

`docs/adr/0001-single-conversion-builder.md` removed the general N-input builder in 2026-06-24 ("116 formulas,
100% pack↔loose pairs, 0 multi-input") and made `/conversions/pair` the sole create+edit screen. Put chose to
**grow that form by one field** rather than restore the builder, keeping the single mental model the ADR bought.

- **Form**: optional `วัสดุแพ็ค` product picker, quantity fixed at 1 per pack; selecting one forces pack-only.
- **`upsert_pack_unpack_pair`**: accepts the extra component and writes `role` on both inputs, through the
  central validator.
- **`derive_pair_from_formula`**: returns `None` for anything with `len(ins) != 1` today
  (`models/conversions.py:298`), so the pencil button dead-ends on a bundle. Teach it the 2-input shape,
  reading **by `role`**, so the Phase 1 formulas reopen prefilled.
- **`bsn_sync.cross_unit_hazard`** (`models/bsn_sync.py:61`) — the guard that forbids a `unit_conversions`
  ratio across a pack/loose pair (the bug that hid for two years). It only looks at `len(ins) == 1`, so it
  cannot see a bundle and 268 ⟷ 270 would fall back to the weaker `pack_piece` branch (which permits ratio 1).
  New rule: single-input formula → the sole input is the partner; multi-input → the `role='component'` row is
  the partner; **multi-input with no role, or more than one component → fail closed as a configuration error,
  never guess**. Matching by "any input whose unit equals the BSN unit" was rejected: two components can share
  a unit.
- Tests (TDD, break-it-once on every guard): role survives a save→derive→save round trip and is **not**
  inferred from row id; `cross_unit_hazard` returns `kind='pair'` for a bundle and raises/fails closed on a
  role-less one; pack-only is forced; the unique index blocks a second active `[แพ็ค]`;
  `_combo_components` still ignores active formulas.

Out of scope for Phase 2: restoring the general builder, changing `run_conversion`'s commit ordering, any
`sku_code` change.

---

## 6. Evidence for the round-1 findings (verified, not relayed)

- **No `ORDER BY` on any input read**: `models/conversions.py:294` (derive), `models/bsn_sync.py:87`
  (cross_unit_hazard), `models/conversions.py:401` (run). Only `get_conversion_formula` orders by `cfi.id`.
  And `sqlite_master` shows **no index at all** on `conversion_formula_inputs` — so today the rows happen to
  come back in rowid order, and adding an ordinary covering index later would silently change that. Row order
  is exactly the kind of implicit contract this codebase has already been burned by.
- **Commit-before-recalc**: `models/conversions.py:485` commits, then `recalculate_waccs_for_products` runs and
  is allowed to raise (its own docstring calls out "real stock movement committed against a stale cost basis").
- **Master upload replaces wholesale**: `_replace_master_tables` runs `DELETE FROM main.<table>` then
  `INSERT ... SELECT * FROM upl.<table>` (`blueprints/admin.py:456-457`) over a list that includes `products`
  — i.e. the derived `cost_price` column for the entire catalogue, not just these 5 SKUs.
- **Duplicate-`[แพ็ค]` invariant holds today**: 6 outputs have 2 active formulas, all of them loose products
  reachable from several packs; **0** outputs have 2 active `[แพ็ค]`.

---

## 7. Verification plan

1. `sqlite3 .backup` a **prod** snapshot; transfer base64 both ways; compare sha256 computed on prod and local.
2. Rehearse Phase 0 → Phase 1 → W4 against the snapshot, and assert, separately:
   - names rebuilt exactly as in the table above, `sku_code` **unchanged** for all four products;
   - `opening_cost` 5 / 71 / 73 **and** a whole-ledger diff before/after for 268, 269, 869 (plus the
     historical margin those rows feed), not just `cost_price`;
   - formulas exist with the right `role` on each input, and the derive path returns the right component
     **with the input rows deliberately inserted in reverse order** (proves order-independence);
   - **`conversion_cost_log.unit_cost` = 71 / 73** and the `CONVERSION_IN` ledger row's `unit_cost` = 71 / 73,
     asserted *separately from* `wacc_after`, which stays 71 / 73 via the negative-stock freeze. Asserting the
     final `cost_price` alone cannot fail, because W2 already produced 71 / 73 — that assertion would pass even
     if the conversion cost lookup did nothing;
   - post-run stock 0 / 0 / 0 / 1,151 / 898, and all three products move atomically;
   - a card shortage writes **no** transaction and **no** cost-log row;
   - a forced WACC preflight failure leaves stock untouched; a forced **post-commit** recalc failure is
     visibly "committed but costing failed" (this is the partial-success path we are gating, so the rehearsal
     must actually exercise it).
3. Apply to prod checkpoint by checkpoint, each verified on a fresh connection with an independent query —
   never the script's own echo.
4. Put runs W4; re-read prod: `/alerts` empty of negative stock, `/conversions` lists both formulas with a
   sane "แปลงได้ตอนนี้", `/products/268` and `/products/269` show the new names and costs.
5. Mirror Phase 0 + Phase 1 to local; compare **values** (names, `opening_cost`, formula rows + roles), never
   row counts.
6. Log the decision in `decisions/log.md`; route the durable findings to memory.

---

## 8. Spun out as separate issues (not fixed here)

1. **`/admin/upload-db` master replace can overwrite prod WACC catalogue-wide.** `products` is replaced
   wholesale while the transaction ledger is preserved, so a stale local `cost_price` — a *derived* column —
   can silently overwrite live prod WACC for every product, not just these five. Mirroring Phase 0/1 to local
   protects *these rows* from being reverted; it does **not** make the upload safe, and the plan no longer
   claims otherwise. Candidate fixes: exclude derived cost columns from the replace, or recalculate WACC for
   affected products after an upload, or block the upload when local/prod derived costs diverge without
   reconciliation.
2. **`run_conversion` commits stock before costing.** A preflight inside `run_conversion` (and the same shape
   in the purchase import) would close the partial-success window for everyone. Deliberately not bundled into
   this task: it changes a shared money path used by 122 formulas.
3. **187 `แผงลูกบิด` carries cost ฿10** while the hammer card is ฿5. If knob cards are also ฿5, that opening
   cost is wrong too. Needs Put's number.

## 9. Review round 2 (Codex) — applied

Verdict: *fix-plan-then-proceed*. Phase order confirmed correct; the two shared money-path issues stay out of
scope. Three corrections, all applied above:

1. **The preflight does not close the partial-success window** — only the identity errors it can see in
   advance. Rewritten as "substantially reduces the known WACC-identity risk; independent post-run
   verification still required", and W4 now carries an explicit one-submit-at-a-time runbook: stop after each
   submit regardless of what the screen says, fresh-read the cost log / transactions / stock, never
   reload-and-retry before that read, and gate run 2 on the verified state from run 1.
2. **The validator's scope is now an explicit invariant** (active `[แพ็ค]%` only), deliberately not keyed on
   `len(inputs) > 1` — which would have quietly outlawed future multi-component manufacturing formulas — with
   the namespace-move rule and the unique index named as its backstop.
3. **"byte-identical rollback" was wrong** and is now "logically identical schema and data", compared via the
   `sqlite_master.sql` text of the affected objects plus the affected rows. SQLite gives no file-level
   byte guarantee.

## 10. Still open

- Nothing blocking. Phase 0's exact migration text and the validator's home (`models/conversions.py` vs a new
  module) are implementation choices to settle when writing the PR.
