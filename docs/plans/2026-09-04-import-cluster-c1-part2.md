# C1 part 2 — move the import-run policy below the route

**Status: CLOSED 2026-09-05 — stopped after part 1, on this plan's own exit
criterion below ("What would make this NOT worth doing"). Put's call.**
Part 1 (one declaration of the register vocabulary) shipped in `ce0c1f8`.
C4 step 2 is **answered and deliberately not built** — see the closing section.
The one real defect this cluster has is not C4; it is tracked separately at
`projects/express-integration/plan-dbf-run-atomicity-2026-09-05.md`.

## Why this is a separate project

`blueprints/bsn.py::express_dbf_upload` is 381 lines and holds six decisions
that are not HTTP concerns. Part 1 removed the one that was safe to move (the
vocabulary). The five that remain are entangled with the request's own
lifecycle, and `sendy_erp` auto-deploys to production on merge, so they are not
a "while I am in here" change.

Evidence that the entanglement is real, from the code itself
(`blueprints/bsn.py:906-908`):

> The SQLite lock cannot be held across the import, because the importer opens
> its own connections and would deadlock against it.

That sentence is the whole problem in one line. The process-level `flock` exists
*because* the modules below cannot share a transaction. Any move of the policy
has to answer that first, which is candidate **C4** (`import_weekly` taking a
`conn`), not this one.

## What is still in the route

| # | Decision | Where |
|---|---|---|
| 1 | what counts as a stale export | `bsn.py:1252-1268` |
| 2 | backup failure is fatal for DBF but not for CSV | `bsn.py:1325` vs `bsn.py:661` |
| 3 | when a future watermark may be healed | `bsn.py:1074-1125`, `:1388` |
| 4 | ~~read back every register~~ | **done in part 1** |
| 5 | ~~which register maps to which page~~ | **done in part 1** |
| 6 | drift-alert record vs clear sequencing | `bsn.py:1465-1483` |

Decisions 1 and 3 are the same concept — *is this file fresh enough, and what
do we do about a clock that disagrees* — and they are the concept that eleven
PRs in four days (#392 … #411, 2026-08-17 to 08-20) kept re-editing in place.
That is the payoff, and also the risk.

## Order of work (each step independently mergeable)

1. **C4 first.** Give `import_weekly` a `conn` parameter and a `try/finally`,
   following `models/mapping.py::repoint_bsn_code`'s existing contract (caller
   owns the transaction; take `BEGIN IMMEDIATE` only when the connection is
   ours). Until this exists, a run cannot be wrapped in one transaction and the
   `flock` cannot be replaced. Needs TDD and a rehearsal on a `.backup`
   snapshot — this is the stock ledger.
2. **Extract a freshness module.** Decisions 1 + 3 into pure functions over
   (export stamp, watermark, now, working-day calendar). No DB, no request.
   The UTC-vs-Bangkok bug (#406) and the future-stamp lockout (#395) both
   become table tests instead of route edits.
3. **Extract the run.** `run(dataset, export_at, force) -> RunResult`, with the
   snapshot policy passed in rather than baked (that is decision 2 — the two
   callers legitimately differ, same as the register policies do).
4. **C6 falls out.** Once the module returns a `RunResult`, the route's 14
   identical 302s can render an outcome instead of flashing Thai text, and the
   JSON blob in `import_log.notes` stops being the only machine-readable
   record.

## Gate before any of it merges

Per `.claude/rules/erp-engineering-discipline.md`: the branch's code has to be
RUNNING in a real Sendy instance, every changed route curled for non-500, and
the real POST exercised — tests alone do not catch werkzeug URL-map or
template-render failures, and `pytest` cannot see bfcache at all.

## What would make this NOT worth doing

If nothing in this cluster is due to change in the next quarter, stop after
part 1. Every candidate here says the code would be *easier to change*; none of
them says the business needs it changed. The Express import works.

---

## C4 findings (step 1 shipped in `9b28f0a`; step 2 not started)

**Step 1 — the connection leak — is done.** `import_weekly` now has the
`try`/`finally` its read-only twin always had. TDD: the test failed on the
pre-fix build with `close_calls == 0`.

**Step 2 — the `conn` parameter — hit two constraints worth writing down
before anyone starts it.**

### 1. The alert writers cannot run inside a caller's transaction

`import_weekly` ends with four best-effort alert writers, and the code states
the rule outright:

> system_alerts' ownership rule: never write the alert on the connection that
> did the work … a second connection opened while this one still holds the
> write lock risks "database is locked"

So under a caller-owned connection the transaction is still open at the end of
the function and those alerts **cannot** be written. A `conn` parameter that
just skips them would lose them silently, which is worse than not having the
parameter. They have to be returned for the caller to write after it commits.

Good news, checked rather than assumed: `templates/import_box.html` accesses
the summary by NAMED key (`r.summary.ignored_detail`), not by iterating
`.items()`, so adding a `pending_alerts` key does **not** change the operator's
results page. The "renders this dict verbatim" comment at the return statement
is about key *names*, not about iteration.

Doing this properly also closes the KNOWN GAP the code already documents: when
the WACC block raises, the alert writers are never reached, so an import can
commit its rows, skip billable lines, and produce no skip alert.

### 2. `conn.rollback()` in the WACC block would roll back the CALLER

Both WACC failure branches call `conn.rollback()`. That is correct while the
function owns the connection. Under a caller-owned connection it would discard
whatever the caller had done before calling in — including, in the C1 part 2
design, the watermark claim and anything else in the same transaction.

This is the part that needs a rehearsal on a `.backup` snapshot rather than
reasoning: the question "what should a WACC identity failure do to a caller's
transaction?" is a money decision, not a refactor detail. Options are to
promote it to a savepoint, to hand the decision to the caller, or to keep WACC
outside the caller's transaction entirely (which is what the current
commit-then-recalculate order effectively does).

**Do not add the `conn` parameter without answering #2.**

---

## C4 step 2 — answered: don't (2026-09-05)

Question asked above: *"what should a WACC identity failure do to a caller's
transaction — savepoint, hand it to the caller, or keep WACC outside?"*

### The answer is already in the codebase, three times

Hand it to the caller. This is not a new decision, it is the house rule, stated
verbatim in three places:

- `models/wacc.py:147` (`recalculate_product_wacc`) — *"When a connection is
  PASSED IN the caller owns rollback and close; this function only propagates."*
- `models/wacc.py:32` (`WaccIdentityError`) — *"Carries structured context so
  whoever OWNS the failed connection can persist an actionable alert after
  rolling back and releasing it."*
- `models/mapping.py:895-916` (`repoint_bsn_code`, the precedent this plan
  points at) — every `rollback()` / `close()` / alert is gated behind `if own:`
  and it re-raises bare, with the reason written down: *"When we do NOT own the
  connection we cannot roll back or close it, so the alert is the supplying
  caller's responsibility."*

So: **not** a savepoint (nothing in this repo uses one for this, and it would
invent a fourth semantic), and **not** "keep WACC outside" (impossible under a
caller-owned connection without committing the caller). Gate the two
`rollback()`/`close()` pairs behind `if own:` and propagate. Constraint #1 above
resolves the same way — under a caller-owned connection the alerts become the
caller's responsibility, exactly as `scripts/remap_bsn_code.py` already does.

Footnote that shrinks #2 further: `preflight_batch` is READ-ONLY and runs before
anything is mutated, so a `WaccIdentityError` raised from it has nothing to roll
back at all.

### But #2 was the wrong question — the real blocker is 20 lines above it

`import_weekly` calls `conn.commit()` at `models/imports.py:424`, **before** the
WACC block. `repoint_bsn_code` has no such mid-function commit; that is why its
contract transplants cleanly and this one does not.

Under a caller-owned connection the two failure modes are not symmetric:

- a wrong `rollback()` is **loud** — the caller can see its own work vanish;
- a mid-function `commit()` is **silent** — the caller's work (the watermark
  claim, in the C1 part 2 design) lands anyway, including when the caller
  fails afterwards and believes it rolled back.

And that commit is deliberate, not incidental. `models/imports.py:426-434` says
so: the source/stock rows are committed first precisely so that *"On failure
every product keeps its previous WACC"* and the operator is told **"นำเข้าสำเร็จ
แต่คำนวณต้นทุนไม่สำเร็จ"**.

Gating it as `if own: conn.commit()` therefore changes a **money-visible
outcome** for the new call shape. Today a WACC identity failure still lets the
import land (rows + stock durable, cost stale, alert raised). Inside a caller's
transaction the caller would unwind the whole import — and `WaccIdentityError`
is a data-state failure, not a transient one, so the import cannot land on retry
either until someone repairs the provenance. On a weekly book that means the
week's sales sit outside the ERP. That is Put's call, not a refactor detail.

### And solving it would still buy nothing today

- `import_weekly` has exactly two production call sites, `import_router.py:167`
  and `:431-432`. **Neither passes a connection**, and nothing is queued to.
  Adding the parameter now is dead code (YAGNI, rung 1 of the ladder).
- The payoff this plan claims for C4 — one transaction per run, `flock`
  replaceable — needs **all 15+ sub-imports** of a DBF run converted, not two:
  `_upsert_invoice_refs`, `import_payment_records` ×2, `run_import_records` ×2,
  `import_credit_note_amounts_records`, `_replace_general_ledger`,
  `_replace_sales_orders`, `_replace_bank_cheques`, the billing-note and
  AR/AP-snapshot writers. Each opens its own connection and commits on its own.
- The `flock` was never the thing standing in the way of atomicity. It serialises
  *concurrent* runs; it says nothing about *partial* ones, and
  `blueprints/bsn.py::_audit_forced_import` already records that
  *"the importer commits in several transactions."*

### What is real, and where it went

A DBF run is **not atomic across its sub-imports** — a purchase import that
fails after the sales import committed leaves a half-applied run, and the
existing recovery is an audit row plus the next day's idempotent re-import.
That is a genuine defect and it is now tracked on its own, with the money
question above as its gate:
`projects/express-integration/plan-dbf-run-atomicity-2026-09-05.md`.

Steps 2-4 of "Order of work" (freshness module, extract the run, C6) are
untouched and still available; they never depended on C4 except through the
`flock` argument, which the bullet above retires.
