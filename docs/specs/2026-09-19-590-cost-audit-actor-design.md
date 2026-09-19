# #590 design: get the actor onto every cost write

The inputs are in the census (`2026-09-19-590-cost-writer-census.md`): 34 entry points,
11 app-side SQL sites, and 0 of 5,467 cost audit rows signed on prod.

The whole problem is that a trigger cannot see who is acting. Each option below answers
where the actor's **value** comes from and where it is **stored**.

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

## Decisions for root / Put

- **D1.** Should an unsigned products INSERT with non-zero cost be *refused* too?
  Recommendation: no, record only. Refusing costs about 50 raw test INSERTs and a master
  upload rework, and a script that creates a product always recalculates next, which is
  refused anyway.
- **D2.** Keep the lazy write-on-read (U3, and U10's `get_current_wacc`) and sign it as the
  viewer? Recommendation: yes for #590. Removing the write-on-read is its own issue.
- **D3.** Should `script_connection` also set `TZ=ICT-7` like `app.py`, so that ssh-run rows
  stop stamping UTC? Recommendation: yes. It is one line and it is a forensics fix.
- **D4.** The #577 regression (census §5): open it as a separate issue, not part of #590.
