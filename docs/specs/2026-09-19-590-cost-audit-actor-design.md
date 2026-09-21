# #590 design: get the actor onto every cost write

**Revision 2 (2026-09-20)** adopts the /interrogate verdict: A1 to A5, C1, C3 and C4, with
C2 decided in §6. Revision 1 is commits ceb7a13 and 1180ceb on this branch.

The inputs are in the census (`2026-09-19-590-cost-writer-census.md`): 35 entry points,
11 app-side SQL sites, and 0 of 5,467 cost audit rows signed on prod.

## 1. Where `manual:<user>` goes today

`product_edit` passes `source=f'manual:{_who}'` to `update_product`. Yet all 155 prod
`audit_log` rows that touch `products` cost since 2026-09-17 have `change_source` NULL and
`user` NULL (read 2026-09-19 16:04Z). **The value is not dropped along the way. It never had
a way into `audit_log`.**

1. `update_product` writes the value into the one-row side table `price_change_source`
   (mig 130).
2. Only `product_price_history_update` reads that side table. So the name lands in
   `product_price_history.source`: pid 27, 2026-09-17 12:04:58, `manual:admin`.
3. `audit_products_update` inserts four columns only: `table_name`, `row_id`, `action`,
   `changed_fields`. Its definition was read from prod's `sqlite_master`. Row 616627 has all
   three provenance columns NULL.
4. `audit_log.change_source` is written only by the six mig-173 sales/purchase triggers.
5. The WACC recalculation that follows runs on a second connection and stamps `wac-sync`.

## 2. What SQLite does, pinned by tests rather than a spike

The print-only spike is deleted. Its claims are now asserting tests in PR 1
(`tests/test_actor_sqlite_contract.py`). They run against the real resolver
`sendy_actor(field)`, and each has a control.

- A persistent trigger may call a function the application registered on the connection.
  `CREATE TRIGGER` does not check that the function exists.
- SQLite resolves the function when it compiles a statement, whatever `WHEN` says.
  - A connection without the function fails on any statement whose trigger program names it.
  - A column-list `UPDATE OF cost_price, opening_cost` trigger therefore leaves raw non-cost
    UPDATEs working.
  - A function inside a products **INSERT or DELETE** trigger would break every raw INSERT
    or DELETE. That covers 259 raw INSERT lines in the tests and the ATTACH master upload.
- `PRAGMA trusted_schema=OFF` gives `unsafe use of sendy_actor()`. Prod reads 1, and
  `get_connection` pins it ON.
- Each connection has its own function. An identity bound to one connection never resolves
  on another.
- **`RAISE(ABORT)` backs out only the statement it fired in.** Earlier statements in the
  same transaction stay, and a caller that catches the error can commit them (see A2).
- `REPLACE INTO` / `INSERT OR REPLACE` never fires `UPDATE OF` triggers. An UPSERT's
  `DO UPDATE` does fire them.

## 3. The design (Option A, revised)

### A1. One actor channel

- **Where the identity lives.** A new module `inventory_app/actor.py` holds one `ContextVar`
  of frames. It has no Flask import.
- **What the SQL function reads.** `sendy_actor(field)` takes `who`, `source` or `reason`.
  It reads **only** that channel plus the identity bound to its own connection. It returns
  NULL on any failure and **never raises**.
- **Requests.** `app.py` registers a `before_request` hook first. It pushes a **root** frame:
  - `who` = the session username, written `ball via admin` while impersonating
    (`_real_username`);
  - kind `ui`, and detail = `request.endpoint`.

  `teardown_request` resets the variable from the token saved in `g`. That reset is
  load-bearing: sync workers reuse the thread, and without it the next request or anything
  outside a request would inherit the last user.
- **`actor.acting_as(...)` nests, and the innermost frame wins.**
  - A frame that names a `kind` is a **root**: a new process identity (script, migration,
    VAT build).
  - A frame with only `detail` or `source` is a **partial** and composes onto the frame
    below it. `detail` chains with ` > `.

  This is how the engine adds *what* (`wacc:<operation>`) while *who* stays the request's.
- **Precedence, from lowest:** the test default, then the identity bound to the connection,
  then the frames. The last root frame discards everything beneath it.
- **The test default** is a lowest-precedence fallback, set by a function-scoped autouse
  fixture in `conftest` (`test` / `pytest` / nodeid). It is not an `acting_as`. So a test
  that sends a request sees the request's `ui` actor, which is what U1 to U11's tests have
  to observe. `set_fallback` refuses to run outside pytest.
- **`database.script_connection(name, operator=, reason=)`**
  - opens a connection with the same settings as `get_connection`;
  - **binds** the script identity to that connection alone (`manual`, operator,
    `script:<file>: <reason>`);
  - refuses a blank operator or reason;
  - **refuses to run inside a Flask app context** (it reads `sys.modules`, so it never
    imports Flask);
  - sets `TZ=ICT-7` with a one-line reason (D3).

  Model functions called from a script must be handed that connection. A connection they
  open themselves resolves nothing and is refused.
- **`sign()` is gone.** There is one SQL function and one declaration scope.
  `actor.install(conn)` only registers the function (used by `get_connection`,
  `script_connection` and the four test fixtures) and declares nothing. The VAT builder
  uses `acting_as('system', …)`.

### A2. Refuse before the first mutation

A trigger refusal is only a backstop, because of the `RAISE(ABORT)` scope in §2. The real
refusal is a **Python preflight at each seam, before its first write**:

- `import_weekly`, `commit_express_dbf`, `run_conversion`, `update_unit_conversion_ratio`,
  `repoint_bsn_code`, `update_product` when it carries cost keys;
- the engine's own entry, before its DELETE.

Each seam reads the actor from its connection (`SELECT sendy_actor('who')`, the same
resolver) and raises `ActorMissing` when there is none.

**These seams commit documents and stock before the WACC step**, which is why the check has
to come first. Today "documents and stock land, then cost is refused" would be the truth.

A refusal leaves a **durable alert** the way `WaccIdentityError` does. The owner of the
connection rolls back, closes, and records the alert on a fresh connection.

### A3. What is guarded (the promise is exactly this list)

| table | INSERT | UPDATE | DELETE |
|---|---|---|---|
| `products` | **ceiling**: recorded by the existing audit trigger with no actor. Covers `INSERT OR REPLACE` / `REPLACE INTO` (never fires UPDATE triggers) | `UPDATE OF cost_price, opening_cost`: a BEFORE guard refuses when a value changes and `sendy_actor('who')` is NULL; an AFTER audit row carries `user`/`change_source`/`change_reason`. An UPSERT `DO UPDATE` is covered | **ceiling**, recorded without an actor |
| `product_cost_ledger` | BEFORE guard through the function | BEFORE guard through the function | BEFORE guard through the function |
| `conversion_cost_log` | the guard, plus `written_by` stamped **by the trigger** from the function. A caller-supplied value is overwritten, so it cannot be spoofed | guarded | guarded |

- **No `written_by` on the ledger.** Every recalculation deletes and re-inserts a product's
  ledger rows, so the column would only name the last recalculator, and it would break
  mig 156's rollback.
- **Instead, the engine writes one recalc-event audit row** before the DELETE, in the same
  transaction: product, operation, actor, and old→new WACC. The engine computes the new
  ledger in memory first.
- **Backfill:** the 40 existing `conversion_cost_log` rows get `written_by='legacy:pre-590'`.

### A4. Expand, then contract

- **PR 1 (expand).** Registers the function on every `get_connection`, pins
  `trusted_schema`, adds the channel, the test default and `script_connection`, and signs
  the four fixtures. It has **no triggers, no refusals and no schema change**, so it cannot
  fail a write.
- **PR 2 (contract).** The migration, the preflights, the audit rows and the census tests.
- **Why the order is safe.** By the time PR 2's triggers exist, every running worker
  (PR 1 code or later) registers the function, so an old and a new deployment overlapping
  is safe.
  - The migration runs once, in the `--preload` master, before the workers accept requests.
  - An import still running in the old container can hold the write lock past the 10-second
    busy timeout; the boot then fails and the old deployment keeps serving. Deploy PR 2
    outside import activity.
- **The migration header states the rollback order:**
  - run `NNN.rollback.sql` **before** reverting any code;
  - never revert PR 1 while PR 2's triggers exist;
  - the rollback restores `audit_products_update` **byte-identically**.

  Rehearse both directions on a `.backup` and diff `sqlite_master`.
- **#615 merges before PR 2**, so the first attributed cost rows do not blame editors for
  that bug.

### A5. Vocabulary: aligned with mig 173, recorded in `CONTEXT.md` by PR 2

| situation | `change_source` | `user` | `change_reason` |
|---|---|---|---|
| UI edit | `manual` | username (`ball via admin`) | `ui:<endpoint>` |
| importer run from the UI | `import` | uploader | `ui:<endpoint> > import:<file>` |
| engine recalculation | the caller's | the caller's | `<caller reason> > wacc:<operation>` |
| operator script or ssh heredoc | `manual` | operator | `script:<file>: <reason>` |
| migration | **`migration`** (new: 173 has no value for this) | `deploy` | `migration:<file>` |
| VAT-book build | `import` | uploader (passed in env) | `system:vat-book-build` |
| whole-file swap (§6) | `manual` | admin, or `bootstrap-token` | `ui:<endpoint>` / `system:bootstrap` |
| tests | **`test`** (new, never on prod) | `pytest` | `test:<nodeid>` |

- **Consistent with the pruner.** `change_source` stays inside 173's `import`/`manual`
  wherever 173 has a meaning. The pruner reads only `import` on sales and purchase INSERTs,
  so `migration` and `test` cannot change retention.
- **The 09-18 question becomes answerable:** "the engine ran, which operation, who set it
  off".
- **A viewer is not blamed.** Someone who opens `/cost-history` is recorded as
  `ui:products.product_cost_history > wacc:lazy_read`, never as someone who typed a cost.

### C1. No `products.created_by`

Callers would supply it, `apply_decision_remaps` copies it forward, and a master upload
preserves it, so it would name the wrong person. Attributing a product INSERT is outside
#590 and becomes a stated ceiling (§5).

### C3. The master upload audits inside its own transaction

`_replace_master_tables` writes the rows **before its own commit**:

- one summary row;
- one row per product whose `cost_price` or `opening_cost` differs between `main` and `upl`.

The actor comes from the request frame. Nothing is written after the commit.

### C4. Test plumbing

These go into the implementation:

- sign the four fixtures (`tmp_db_conn`, `tmp_db_conn_hr_clean`, `empty_db_conn`,
  `patch_models_conn`);
- build the schema census from **`data/schema.sql`**, not `LIVE_DB`;
- assert **a behavioural refusal per guarded column and verb**, never trigger text;
- allowlist `pending_product_suggestions.suggested_cost` (a suggestion, not a cost of
  record) and the `products_full` view (read-only), each with its reason.

## 4. Rejected alternatives

- **B, declared on the row (mig 173's pattern).** It would thread the actor through
  14 functions, 10 routes, about 12 scripts and about 80 test files, and one missed call
  site blocks that write.
- **C, an app-side choke point plus a static sweep only.** A raw ssh script still writes
  unsigned.
- **Codex's "refuse non-zero-cost product INSERTs".** A function in a products INSERT
  trigger breaks every raw INSERT (§2). It stays a ceiling.

## 5. Ceiling

1. **Identity is declared, not authenticated.** There is one Railway account, and a script
   can register its own `sendy_actor`. This stops unsigned writes that happen by accident,
   not a deliberate bypass. Mig 173 has the same limit.
2. **Products INSERT, `REPLACE INTO products` and products DELETE are recorded but not
   refused, and carry no actor.** The master upload (C3) and whole-file swaps (§6) write
   their own signed rows.
3. **Non-Python clients cannot change cost at all.** The sqlite3 CLI fails
   `no such function`. That is deliberate.
4. **`trusted_schema` must stay ON.** `get_connection` pins it, and a test proves it.

## 6. C2 decided: migrate the staged file before the swap; no master re-exec

**The problem.** Four routes replace the whole DB file:

- the full upload (`admin.py:615`);
- the upload confirm (`admin.py:661`);
- backup restore (`db_backup.restore_backup`, `:398`);
- `/bootstrap/upload-db` (`app.py:231`).

Today they then send `SIGHUP`. Under `--preload` that does not re-run `init_db`, so an older
file would stay unmigrated and **unguarded** until a real restart.

**The decision.** Each route stages the incoming file and calls
`database.prepare_staged_db(path, actor)`, which:

1. runs the boot migration path on the staged file (`init_db` gains a `db_path` argument);
2. **validates by behaviour**: an unsigned cost UPDATE on the staged file must be refused
   (then rolled back), and `applied_migrations` must contain every repo migration;
3. stamps one signed audit row describing the swap into the staged file;
4. **refuses the swap** if any step fails.

Only then does `os.replace` run, followed by today's `SIGHUP`.

**Why not re-exec the master.** It leaves the swapped-in, unguarded file live until the
restart completes. It depends on Railway restart semantics we have not verified. A failed
restart takes the app down.

This also answers Codex 6: the swap's audit row lives in the file that goes live. Migrating
a restored backup forward is exactly what a restart does today, so the meaning of a restore
does not change.

## 7. Coverage: a new unsigned writer has to go red

1. **Schema census.** Load `data/schema.sql`. For each cost column in §A3 and each guarded
   verb, show that an unsigned write is refused and a signed one passes. Break-it-once:
   drop each guard. A cost-named column with no guard and no allowlist reason goes red.
2. **Writer census.** Shaped like `test_revenue_filter_coverage.py`. The shapes, each with a
   rogue file: f-string table, `%`, `.format`, `+` concatenation, `main.`-qualified,
   `INSERT … SELECT`, **copy-all-columns INSERT, `REPLACE INTO`, `INSERT OR REPLACE`,
   UPSERT**, `UPDATE OR REPLACE`.
3. **Behaviour.**
   - U1 to U11 each land `manual`/`import`, the username and the endpoint on the audit row.
   - Each A2 seam refuses **before** its first write: no document, stock or ledger row
     lands, and an alert exists.
   - The VAT build and the migration come out signed.
   - The four swap routes refuse an unmigratable file.
   - A real UI cost edit through `verify-sendy` is asserted on the audit row.
4. **PR 1's own tests.** The resolver returns the right actor in each context: request,
   nested scope, script, test default, and the teardown reset with no leak across two
   sequential requests. Each has a break-it-once.

## 8. Migration plan for callers outside this repo's tests

Checked 2026-09-19 16:18Z, against `origin/main` 21a58e4, every branch on origin, the local
worktrees and the brain repo. Revision 2 changes it in two places: no ledger `written_by`,
and the ledger and conversion log are guarded through the function.

### What starts failing, and how

| caller | after PR 2 |
|---|---|
| A raw connection or the sqlite3 CLI that SETs `cost_price`/`opening_cost`, or writes the ledger or conversion log in any way | refused when the statement compiles: `no such function: sendy_actor` |
| A signed connection with no declared actor (a script that forgot, a heredoc) | the A2 preflight raises `ActorMissing` before the first write, plus an alert. The trigger is only a backstop |
| Reads, non-cost UPDATEs of `products`, products INSERT/REPLACE/DELETE (ceiling), `.backup`, VACUUM, `dump_schema.py` | unchanged |

### `scripts/`

Mig 173's decision holds: live tools are fixed, and dated one-offs stay byte-identical and
are accepted to abort if re-run.

| script | needs |
|---|---|
| `merge_product.py`, `remap_bsn_code.py` (live tools) | **fixed in PR 2**: `script_connection(__file__, operator=--operator, reason=--reason)`, both arguments required. Their tests pass `--operator` |
| `2026_09_19_gross_to_piece.py` (its `rebase()` is an engine that S3 loads, and it could be reused for #599/#600) | **fixed in PR 2**: `main()` uses `script_connection`, and `rebase()` requires a signed connection |
| S1, S3, S4, S5, S8, S9, S10, S11 (dated one-offs, already applied) | **no edit**. Their 9 test files call `tests/_pre_mig590.py::emulate_pre_mig590(db)`, which drops **all** #590 triggers and returns how many it found. The recalc-event row resolves through conftest's test default. `test_pre_mig590_usage.py` pins exactly those 9 files, with a reason each |
| S7, S12, S13, S18 (dated) | no edit. They abort if re-run, which is accepted |
| S15, S16, S17 (INSERT only) | no edit. Products INSERT is a ceiling |

### Brain repo

| caller | needs |
|---|---|
| `add-loose-variant/pack_loose_variant.py` | keeps working (it only inserts). Switch to `script_connection` the day of the merge |
| agents `payments-finance.md`, `data-quality.md` | one line pointing at the rule |
| dated prod one-offs and old `.backup` WACC probes | none. Any **future** rehearsal on a copy must sign, which the rule states |

### Announcing the rule

The PR 2 body carries a "⚠ Breaking for ops" block. The brain-repo commit to
`.claude/rules/erp-engineering-discipline.md` lands **the day PR 2 is prod-verified**, and
the PR checklist names its sha. It is an exception inside the ad-hoc SQL bullet:
`sqlite3 "$DB" < file.sql` stays correct for every non-cost write.

Draft text:

> **Cost writes must be signed (#590, mig NNN).** Any write to `products.cost_price` /
> `opening_cost`, `product_cost_ledger` or `conversion_cost_log` needs a declared actor.
>
> - **Inside the app** the session supplies it.
> - **Everywhere else**, including scripts, `railway ssh` heredocs and rehearsals on a
>   `.backup`, open the connection with
>   `database.script_connection(__file__, operator='<who>', reason='<why>')` and pass it to
>   every model call.
>
> ⛔ **The sqlite3 CLI can no longer change cost** (`no such function: sendy_actor`). Never
> register a stand-in function to get past it. Rehearse a cost-touching migration through
> the runner (a `verify-sendy` launch, or `init_db` on the copy), not with
> `sqlite3 copy.db < NNN.sql`.

### Open branches (read 2026-09-19 16:18Z)

- **The branches you named:**
  - `feat/596`, `feat/597` and `fix/592` are merged (#612, #609, #614).
  - #599, #600 and #610 have **no branch**. Their "WACC unchanged on a `.backup`"
    rehearsals must sign if PR 2 lands first.
- **Open PRs:** 0.
- **Local only:**
  - `fix/615` should merge before PR 2. After it lands, PR 2 signs its raw `UPDATE … cost_price`.
  - `fix/594` takes migration 188, so PR 2's number is 189 or later, derived from
    `origin/main` when the file is created.

## 9. Decisions (root, 2026-09-19)

- **D1. Record only.** Now moot: `created_by` is dropped (C1) and products INSERT is a ceiling.
- **D2. Sign the lazy GET recalc as the viewer.** The A5 detail makes that honest.
  Removing write-on-read is a separate issue.
- **D3.** `script_connection` sets `TZ=ICT-7` and carries a one-line reason.
- **D4. Filed as #615.**

## 10. Implementation notes (PR 2, where the code departs from the text above)

Each of these was found while building or break-testing PR 2. The design intent holds in
every case.

- **Migration number 191.** 188 (#617) and 189 (#623) are on main; 190 is claimed by
  #628 (`feat/599-express-meanings`), still open on 2026-09-21. Re-derive the number at
  rebase. The runner applies by filename, so if #628 merges after this PR its 190 runs
  after 191 on prod. That is harmless: the runner's connection is signed.
- **Scripts that reach the cost engine without cost SQL of their own** (#603's rebase
  script, which drives the gross_to_piece engine) are invisible to the writer sweep. A
  third census pins each one as signed or dated (`test_cost_writer_census.py` §3).
- **The recalc-event row is written after the walk, not before the DELETE.** It is still
  in the same transaction, just before the ledger INSERTs, which is the first point where
  old→new is known. Moving the DELETE would have reordered a money path for no gain in
  atomicity.
- **A caller-supplied `written_by` is refused, not overwritten.** "Overwrite, then never
  rewrite" cannot be expressed: the stamp trigger's own UPDATE would hit the immutability
  guard. Refusing is stricter.
- **An unsigned conversion-log INSERT is refused twice**, by its own guard and by the
  update guard that the stamp trigger fires. Break-it-once therefore deletes both (G3b).
- **Dated scripts' tests** register `sendy_actor()` on every raw connection
  (`tests/_pre_mig590.py::sign_raw_connections`), rather than dropping the triggers.
  Dropping them would not be enough: the engine's A2 preflight refuses a raw connection
  as well. Both the triggers and the preflight stay live in those tests, signed by the
  test default. `test_pre_mig590_usage.py` pins the three files.
- **`db_backup.restore_backup` takes a required `prepare=` hook**, so no caller can
  forget it. The toy-DB unit tests pass `prepare=None`.
- **The VAT build receives the uploader as `--uploader`** (read from the request's
  actor) instead of an environment variable. With no uploader nothing is declared, and
  the importers refuse, as for any unsigned run.
- **`import_weekly` is a thin wrapper** declaring `source=import` around
  `_import_weekly`. Two per-function sweeps were re-keyed to follow the body.
