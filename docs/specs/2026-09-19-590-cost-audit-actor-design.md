# #590 design: get the actor onto every cost write

The inputs are in the census (`2026-09-19-590-cost-writer-census.md`): 34 entry points,
11 app-side SQL sites, and 0 of 5,467 cost audit rows signed on prod.

The whole problem is that a trigger cannot see who is acting. Each option below answers
where the actor's **value** comes from and where it is **stored**.

## Where `manual:<user>` goes today

`product_edit` passes `source=f'manual:{_who}'` to `update_product`, yet every one of the 155
prod `audit_log` rows that touch `products` cost since 2026-09-17 has `change_source` NULL
and `user` NULL (prod, read 2026-09-19 16:04Z). **The value is not dropped along the way. It
never had a way into `audit_log`.**

1. `update_product` hands `source` to `_set_price_change_source(conn, source)`. That writes
   the one-row side table `price_change_source` (mig 130), on the same connection and in the
   same transaction, just before the UPDATE. Right after the UPDATE it sets the value back
   to NULL.
2. The UPDATE fires two AFTER UPDATE triggers on `products`. **Only one of them reads the
   side table.**
   - `product_price_history_update` inserts into `product_price_history (…, source)` with
     `(SELECT source FROM price_change_source WHERE id = 1)`. So the name does land, but
     only in `product_price_history.source`. For pid 27 at 2026-09-17 12:04:58 that is
     `cost_price 33.0→48.5`, source `manual:admin`.
   - `audit_products_update` inserts into `audit_log (table_name, row_id, action,
     changed_fields)`. Those are its only four columns, read from prod's `sqlite_master`
     rather than from the repo. It never mentions `user`, `change_source` or
     `price_change_source`. The same edit's audit row, id 616627, has all three
     provenance columns NULL.
3. `audit_log.change_source` has existed since mig 173, but the only triggers that write it
   are the six sales/purchase audit triggers, which copy `NEW.change_source` off the
   document row. No `products` trigger was ever taught about it.
4. The WACC recalculation that follows the edit runs on a second connection and stamps
   `wac-sync`. So even `product_price_history` credits the resulting cost to the engine,
   not to the person.

Option A below fixes this at the source: the new cost audit trigger writes `user` /
`change_source` / `change_reason` from `sendy_actor()`. `price_change_source` can then go
back to its only job, the price-history label, and needs no further trust.

## What a spike established

Spike D, `2026-09-19-590-spike-actor-function.py`. It ran on local SQLite 3.51.0 and on
prod's Python with SQLite 3.46.1, piped over `railway ssh`, in memory only.

- A persistent trigger may call a function the application registered on the connection
  with `create_function`. `CREATE TRIGGER` does not check that the function exists.
- The function belongs to one connection. With A signed and B unsigned, B's cost write was
  refused, so a signature never crosses to another connection. That is exactly the leak that
  sank the shared-row design (spike A) behind mig 173. Spike B had shown that TEMP tables are
  impossible (`trigger cannot reference objects in database temp`). A function is the
  per-connection mechanism mig 173 wanted and could not have.
- A connection that has not registered the function fails on **any statement whose trigger
  program mentions it**. SQLite resolves functions when it compiles the statement, whatever
  the `WHEN` clause says. The consequences:
  - A column-list trigger (`UPDATE OF cost_price, opening_cost`) leaves a raw
    `UPDATE products SET product_name=…` working, and makes a raw cost update fail with
    `no such function: sendy_actor`.
  - A function inside a products **INSERT or DELETE** trigger would break every raw INSERT
    or DELETE: 259 raw INSERT lines in 142 test files, and the admin master upload.
- With `PRAGMA trusted_schema=OFF` the trigger fails with `unsafe use of sendy_actor()`.
  Prod reads 1 today.
- 1,000 updates under one transaction produced 1,000 signed audit rows.

## Option A (recommended): a signed connection, where the actor is resolved when the write happens

- `database.get_connection()` registers `sendy_actor(field)` and pins
  `PRAGMA trusted_schema=ON`. `database.sign(conn)` does the same for the raw connections
  the app itself opens (the master upload, the VAT-book builder).
- The function looks up the actor **when the trigger fires**, in this order:
  1. the innermost `with database.acting_as(kind, who, detail)`, a contextvar;
  2. otherwise the Flask request: `ui`, the session username
     (`+ " via " + _real_username` while impersonating), and `request.endpoint`;
  3. otherwise NULL.

  `acting_as('script', who='')` raises. Scripts open their connection through
  `database.script_connection(__file__, operator=…, reason=…)`.
- Where the actor is stored:
  - **`audit_log.user`** holds who, **`change_source`** holds the kind
    (`ui` / `script` / `migration` / `system` / `test`), and **`change_reason`** holds the
    detail (endpoint, script plus reason, migration filename). These columns already exist,
    so `audit_log` needs no schema change.
  - **`product_cost_ledger.written_by`** and **`conversion_cost_log.written_by`** are new
    columns. The engine's INSERT writes `sendy_actor(...)` straight into its `VALUES`.
  - **`products.created_by`** is a new column, stamped the same way by
    `create_structured_product`.
- Triggers, all in one new migration (number taken from `origin/main` at creation):
  - **products**: move `cost_price` out of `audit_products_update` and into a new pair of
    column-list triggers on `UPDATE OF cost_price, opening_cost`:
    - a BEFORE guard that raises with a Thai message when a value changes and there is no
      actor;
    - an AFTER audit row carrying the actor.

    This also puts **`opening_cost`** under audit for the first time. The products INSERT
    audit trigger copies `NEW.created_by` and never calls the function.
  - **ledger and conversion log**: a BEFORE INSERT guard on `written_by IS NULL OR ''`. It
    reads the column only, so raw INSERTs still compile and a failure message is readable.
    The rescale scripts' ledger UPDATE (S1 to S3) also gets a column-list guard and audit.
- Every process that writes with no request has to declare an actor:
  - the migration runner wraps each file in `acting_as('migration','deploy',<file>)`;
  - U5 passes the uploader to the VAT-book subprocess through `SENDY_ACTOR`, and the
    builder declares `system`. Its vat_book.db is built from `schema.sql`, so it carries the
    guards and **breaks if it is left unsigned**;
  - tests get an autouse `acting_as('test','pytest',<nodeid>)` in `conftest`, with an
    opt-out marker for the tests that prove the refusal. About 18 raw-SQL cost writes in
    9 test files have to call `sign(conn)`.
- A DELETE and a whole-file swap (U11 to U13) cannot be seen by any trigger. The upload and
  restore routes write the signed audit rows themselves: one summary row, plus one row per
  product whose cost changed for U11.

## Option B: declared on the row (the pattern mig 173 uses)

- `products` gets `cost_actor`, `cost_source` and `cost_token` columns. A BEFORE UPDATE OF
  guard demands a fresh token, like `declared_update`. The ledger and the conversion log get
  `written_by` plus the same INSERT guard as in A.
- The actor travels as an **explicit argument**. It has to be threaded through
  `update_product`, `create_structured_product`, `recalculate_product_wacc`,
  `recalculate_waccs_for_products`, `get_current_wacc`, `get_cost_history`,
  `import_weekly`, `commit_file`, `commit_express_dbf`, `repoint_bsn_code`,
  `update_unit_conversion_ratio`, `run_conversion`, `approve_pending_suggestion` and
  `create_now`. That means their 10 routes (U1 to U10), about 12 live scripts, and about
  80 test files (from a grep of the function names).

## Option C (rejected): an app-side choke point plus a static sweep only

A raw connection over `railway ssh` still writes unsigned, which fails item 3 of the issue,
and so it would miss the 09-18 incident again.

## How A and B compare

| criterion | A: signed connection | B: declared on the row |
|---|---|---|
| gunicorn -w 2 | Resolved from the request context at write time, with no state shared between workers (spike: no crossing) | Per row, no shared state |
| Bulk importer in one transaction | Always signed inside a request. Outside one, the whole transaction aborts (fails closed). The real importer and the VAT build must be rehearsed on a snapshot with the guards installed | Every importer has to thread the actor. If one forgets, the whole file aborts |
| WACC recalculated indirectly | Inherits the actor of whatever is running, with no threading | Needs the argument through about 14 functions |
| Scripts over `railway ssh` | Raw connection: `no such function` (closed but cryptic). `get_connection` with no `acting_as`: a Thai refusal | Thai refusal. Also usable from the sqlite3 CLI by filling the columns by hand |
| UI vs script (item 2) | `change_source` = kind, taken from the context | An explicit `source` argument |
| Lazy recalculation on GET | Signed as the viewer, with the endpoint as the detail. Honest | Needs the argument threaded into readers |
| Hot-path cost | One Python callback per trigger firing (about 5k for a full replay) | A fresh-token protocol on every cost UPDATE |
| Coverage | The guard triggers enforce it. The census test documents it | Guards plus every call site |

**Recommendation: A.** The actor is always present at runtime, in the request or in the
declared script context, and A reads it from there. B would carry it by hand through 14
functions and about 80 test files, and one missed call site is an outage. A's cost is a
dependency on SQLite behaviour, which the spike measured and a test can pin.

**Ceiling of A:**

1. Identity is **declared, not authenticated**. There is one Railway account, and a script
   can register its own `sendy_actor`. A stops accidental unsigned writes, not a deliberate
   bypass. Mig 173 has the same ceiling.
2. Only these are **refused**:
   - an UPDATE of `cost_price` or `opening_cost`;
   - an INSERT or UPDATE of the ledger and the conversion log.

   A products INSERT is recorded through `created_by` but not refused. A DELETE or a file
   swap is recorded only by the app code in U11 to U13.
3. Non-Python clients cannot write cost at all. That is deliberate: the escape hatch is a
   signed Python connection.
4. It depends on `trusted_schema=ON`, which A pins in `get_connection` and a test.

Upgrade path: sign other tables the same way, one table at a time, and later move the
mig-173 tables onto the function to drop their token protocol. Neither is in scope.

## Coverage: a new unsigned writer has to go red

1. **Schema census.** Take every column matching `cost|wacc` from the live schema and assert
   a guard trigger covers it. A future cost-named column without a guard goes red.
   Break-it-once: drop each guard.
2. **Writer census.** Shaped like `test_revenue_filter_coverage.py`: every SQL write to a
   cost table or column in `inventory_app/` and `scripts/`, each with a written reason and
   its signing mode. The sweep has to see every shape: f-string table, `%`, `.format`,
   concatenation, `main.`-qualified, `INSERT…SELECT`, `UPDATE OR REPLACE`.
   Break-it-once with one rogue file per shape.
3. **Behaviour.**
   - U1 to U11 each land `ui`, the username and the endpoint on the audit or ledger row.
   - An unsigned `get_connection` gets the Thai refusal, and a raw connection gets
     `OperationalError`.
   - The migration and the VAT build come out signed.
   - A real UI cost edit through `verify-sendy` is asserted on the audit row.

## Migration plan for callers outside this repo's tests

Checked 2026-09-19 16:18Z against `origin/main` 21a58e4, every branch on origin, the local
worktrees, and the brain repo.

### What starts failing, and how

| caller | after the merge |
|---|---|
| Raw `sqlite3.connect` (or the sqlite3 CLI) running a statement that SETs `products.cost_price` / `opening_cost` | Refused before it runs: `no such function: sendy_actor`. This happens even when the new value equals the old one |
| Any connection, signed or not, that writes a ledger or conversion-log row with no actor declared | Refused by the guard with a Thai message |
| A signed connection with no `acting_as` and no request (a script or heredoc that forgot to declare) | Refused by the guard with a Thai message |
| Reads, non-cost UPDATEs of `products`, product INSERTs (D1: recorded, not refused), `.backup`, VACUUM, `dump_schema.py` | Unchanged |

**One amendment to A, found while doing this plan.** The WACC engine (L5) and `run_conversion`
(L7) should **bind `database.current_actor()` as a parameter**. The note above has them call
`sendy_actor()` inside their SQL. The function then appears only in the two
`products`-cost triggers, and the ledger and conversion-log guards read a column. Two things
follow:

- An unsigned recalculation on a raw connection hits the ledger guard first, on the first
  INSERT of the rebuild, and gets the readable Thai refusal instead of `no such function`.
- The mig-173 precedent (below) becomes possible for the dated scripts' tests.

The value comes from the same resolver either way.

### `scripts/`: what each one needs

Mig 173 already had to handle scripts written before its guard. It recorded the decision
**live tools are fixed; dated one-offs are left byte-identical and accepted to abort if
re-run**. The mechanism is `tests/_pre_mig173.py` and `test_pre_mig173_usage.py`. #590
reuses that decision and that shape.

| script | kind | fails how after merge | needs |
|---|---|---|---|
| `merge_product.py` (S6) | **live tool** | its raw connection reaches recalc → `no such function` | **fix in the PR**: `script_connection(__file__, operator=--operator, reason=--reason)`, both CLI arguments required. Tests: `test_merge_product*.py` pass `--operator` |
| `remap_bsn_code.py` (S14) | **live tool** | same, via `repoint_bsn_code` | **fix in the PR**, same shape. Tests: `test_remap_bsn_code.py`, `test_repoint_bsn_code.py` |
| `2026_09_19_gross_to_piece.py` (S2) | dated, but `rebase()` is an engine that S3 loads, and a future unit rebase (#599/#600) could load it | `UPDATE products SET cost_price` on a raw connection | **fix in the PR**: `main()` opens `script_connection`. `rebase(conn, …)` requires a signed connection, and a raw one fails at the guard, which is fine |
| `2026_08_17_bolt_dozen_to_piece.py` (S1), `2026_09_19_rebase_689_767.py` (S3), `2026_09_19_fix_pack_ratios_592.py` (S4), `2026_09_19_split_belco_582.py` (S5), `hammer_bundle_datafix.py` (S8), `force_stock_targets.py` (S9), `rebuild_opening_balance_v2.py` (S10), `rebuild_opening_balance_from_csv.py` (S11) | dated one-offs, all already applied (their docstrings say "do not re-run") | abort if re-run. **Accepted**, not overlooked | **no edit**. Their 9 test files (`test_bolt_dozen_to_piece`, `test_gross_to_piece_rebase`, `test_rebase_689_767`, `test_fix_pack_ratios_592`, `test_split_belco_582`, `test_hammer_bundle_datafix`, `test_force_stock_targets`, `test_rebuild_opening_balance_v2`, `test_rebuild_opening_balance`) call a new `tests/_pre_mig590.py::emulate_pre_mig590(db)`. It drops only the two `products`-cost triggers and returns how many it found; the ledger guard stays and is fed by conftest's `acting_as('test')`. `test_pre_mig590_usage.py` lists exactly those 9 files, with a reason each |
| `backfill_opening_cost_20260617.py` (S7) | dated, uses `get_connection` | guard refusal (no declared actor) | no edit, no test. Accepted |
| `phase_c_replay_apply_20260530.py` / `phase_c_dedup_replay_20260530.py` (S12/S13), `cleanup_split_mapping_stubs.py` (S18, DEPRECATED) | dated | `no such function` | no edit. They already sit in `SCRIPT_EXEMPTIONS`-style lists as accepted-to-abort |
| `apply_decision_remaps.py` (S15), `apply_platform_overview_mapping.py` / `import_listing_mapping_csv.py` (S16/S17) | INSERT only | nothing: an INSERT is recorded, not refused (D1). `created_by` stays NULL | no edit |

`scripts/` gains nothing new. `script_connection` lives in `database.py`.

### Callers in the brain repo (`~/Sendai-Boonsawat`, a separate repo)

Found with `grep -r`, which does not skip gitignored trees, so `Design/`, `E-Commerce/` and
`Operations/` were covered. Control: the same pattern found wacc.py's 11 hits.

| caller | status | needs |
|---|---|---|
| `.claude/skills/add-loose-variant/pack_loose_variant.py` | live skill. INSERTs a loose product with `cost_price` through `models.get_connection` | keeps working (D1). Switch it to `script_connection` so the creation is signed. It is a brain-repo edit, made the same day as the merge |
| `.claude/agents/payments-finance.md` (its "cost_price backfill" job) and `data-quality.md` | agent policy that writes cost | add one line pointing at the new rule |
| `Operations/06_tools/apply_group_a_2026-07-23.py`, `projects/express-integration/prod_cost_load_*.py` (June), `archives/**` | dated prod one-offs | none. They abort if re-run, which is intended |
| WACC probes that replay on a `.backup` copy: `Operations/05_analysis-reports/finance/wacc_zero_stock_purchase_2026-09-16/replay.py`, `…/tiktok_legacy_cost_basis_2026-09-15/{q3sim*,q4,robin/*}.py` | dated analysis. They call the engine on a raw connection | none for the old ones. **Any future rehearsal on a copy taken after the merge must sign**. The rule text below says so, because "rehearse on a `.backup`" is mandated by the rules and that step recalculates |
| `Operations/06_tools/restock_list.py`, `E-Commerce/TikTok/01_product-info/test_prod_read.py` | read-only, or its own throwaway schema | none |

### Announcing the rule

**Where it goes.** The PR body carries a "⚠ Breaking for ops" block: the table above plus the
rule text below. The rule text lands in the **brain repo** `.claude/rules/erp-engineering-discipline.md`:

- **when:** in a brain commit made **after the merge is prod-verified**, the same day. Earlier
  would describe behaviour that is not live yet. Later would leave every session running a
  recipe that errors.
- **what:** it is an **exception inside the "Applying ad-hoc SQL to the live DB" bullet**,
  not a replacement. `sqlite3 "$DB" < file.sql` stays correct for every non-cost write.
- **tracking:** the PR checklist names the brain commit sha.

Draft text:

> **Cost writes must be signed (#590, mig NNN).** Any write to `products.cost_price` /
> `opening_cost`, `product_cost_ledger` or `conversion_cost_log` needs a declared actor.
>
> - **Inside the app** the logged-in session supplies it.
> - **Everywhere else**, including a script, a `railway ssh` heredoc, or a rehearsal on a
>   `.backup` copy, open the connection with
>   `database.script_connection(__file__, operator='<who>', reason='<why>')`.
>   It also sets `TZ=ICT-7`, so the row timestamps are Bangkok time like the app's.
>
> ⛔ **The sqlite3 CLI can no longer change cost.**
>
> - `SET cost_price` / `SET opening_cost` fails with `no such function: sendy_actor`.
> - A ledger or conversion-log INSERT with no actor is refused.
> - That is the guard working. Never register a stand-in `sendy_actor` to get past it.
>
> `sqlite3 "$DB" < file.sql` stays the recipe for every other ad-hoc SQL. Rehearse a
> migration that touches cost **through the runner** (a `verify-sendy` launch, or `init_db()`
> on the copy), not with `sqlite3 copy.db < NNN.sql`.

### Open branches that write cost (read 2026-09-19 16:18Z)

- **The branches you named:**
  - `feat/596-unit-map-db`, `feat/597-unit-code-cleanup` and `fix/592-pack-ratios` are
    **already merged**: #612, #609 and #614. Their refs are squash leftovers and there is
    nothing to conflict with.
  - **No branch exists for #599, #600 or #610**, on origin or in any local worktree. All
    three are open issues.
  - Each of those issues has an acceptance criterion "rehearsed on a prod `.backup`: WACC
    unchanged". So their rehearsal harness recalculates, and it **must sign if #590 merges
    first**. If they merge first, #590's guarded rehearsal has to include their migrations.
    Neither order causes a textual conflict.
- **Open PRs: 0.** Branches on origin that are not merged and touch cost: **none**.
  - `feat/582-split-belco`, `feat/586-rebase-689-767`, `fix/586-epoch-skip-unit-rebase`
    and `feat/unit-rebase-1050-1320` are merged: #607, #608, #613, #584.
  - `feat/598-unit-writer-census` adds one test file only, with no cost writes, and does
    not overlap.
- **Local only, not pushed:**
  - **`fix/615-product-edit-cost-overwrite`** (worktree `615-cost-box-overwrite`) edits
    `product_edit` and `tests/test_product_edit_cost_basis.py`. That test file already does
    a raw `UPDATE products SET cost_price=99.0` (about line 247) and calls
    `models.recalculate_product_wacc` outside a request. **#615 should merge first.** #590
    then signs that raw UPDATE with `sign(conn)` and relies on conftest for the recalc. The
    conflict is semantic, not textual.
  - **`fix/594-unflag-904`** takes migration **188**, so #590 becomes 189 or later. The
    number is derived from `origin/main` when the file is created and again before the push.
  - `feat/products-new-clone` and `feat/price-lookup-promo-hygiene` are stale (August,
    111+ commits behind) and only read cost.

## Decisions (root, 2026-09-19)

- **D1: record only**, agreed. An unsigned products INSERT with non-zero cost is recorded
  (`created_by`), not refused.
- **D2: sign the lazy GET recalc as the viewer**, agreed. Removing write-on-read is a
  separate issue.
- **D3: yes.** `script_connection` sets `TZ=ICT-7`, with a one-line reason in the code:
  without it, a row written over ssh is stamped UTC while the app's rows are ICT.
- **D4: filed as #615.** A separate Codex lane is on it, and it stays out of #590.
