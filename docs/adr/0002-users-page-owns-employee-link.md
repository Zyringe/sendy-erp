# ADR 0002 — `/users` is the sole editor of the employee↔login link

Status: Accepted · 2026-06-29

## Context

A login account (`users`) and an HR employee record (`employees`) are two different
things (a credential vs a person). They are related 1:1 and optionally: an employee may
have no login (e.g. ยกของ/ขับรถ staff who never touch the system), and a login may have no
employee (test/system accounts). The relationship is stored as **`employees.user_id →
users.id`** — the FK physically lives on the `employees` side.

Before this change:

- The link was editable **only on the HR employee pages** (`/hr/employees/new` and the
  employee detail edit form), via a "บัญชีผู้ใช้ (login)" dropdown that wrote `user_id`.
- The `/users` admin page showed **no** employee at all — there was no way to tell which
  person owned an account, and no way to attach an employee when creating one.
- Deleting a linked account raised a SQLite FK error (no `ON DELETE` rule, `foreign_keys
  = ON`) → an unhandled 500.

The owner asked to manage the link from `/users` (accounts are often created before the
employee record exists, so the link must be set *later*), and considered moving the FK
column itself onto `users`.

## Decision

Make **`/users` the single editor** of the link; keep the **column where it is**
(`employees.user_id`).

- `/users` create + edit forms get an optional "พนักงาน" picker. The write path
  (`_set_account_employee`) is integrity-safe: it unlinks any employee currently on the
  account, then links the chosen one **only if free** (`WHERE user_id IS NULL`), so a
  forged/stale id can never steal an employee linked elsewhere. `get_linkable_employees`
  is the single source of the 1:1 selection rule.
- The HR pages **stop editing** the link: `create_employee`/`update_employee` no longer
  bind `user_id` (an HR edit must never wipe the link), and the dropdown is replaced by a
  **read-only** display + a link to `/users`.
- `user_delete` auto-unlinks (`UPDATE employees SET user_id=NULL`) before deleting,
  fixing the latent FK-500.
- The five roles are defined once in `app.py::ROLES` (label / badge / icon / description);
  badges, the `/users` permission summary, and the topbar all render from it.

We **did not** move the column to `users.employee_id`. That would be a schema migration
plus a rewrite of every consumer of `employees.user_id` — the live HR self-service flow
(`me._my_employee` → `get_employee_by_user_id`), `get_linkable_employees`, employee
create/update — for **zero** functional gain (the editing UX is identical regardless of
which table holds the column).

## Consequences

- "Where the link is edited" (`/users`) deliberately differs from "where the column
  lives" (`employees`). This ADR exists so that difference doesn't read as an accident.
- One write path → no two-editor drift. The HR side is display-only and points to `/users`.
- Adding/renaming a role is a one-line edit in `ROLES`; the badge and summary can't
  disagree. The summary text is still kept in sync with the enforcement code (POST
  whitelists + GET gating) **by hand** — a known, accepted residual (auto-deriving it from
  the whitelists was judged over-engineering for now).
- If the column is ever relocated to `users.employee_id`, it must be a separate, isolated
  migration (rewriting the self-service path), never bundled with UI work.
