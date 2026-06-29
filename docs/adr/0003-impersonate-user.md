# ADR 0003 — Impersonate a *user* (full act-as), not just simulate a *role*

Status: Accepted · 2026-06-29

## Context

The `/users` page had a "จำลอง" (simulate) button per non-admin row. It swapped only
`session['role']` (the permission level) and kept the admin's own `session['user_id']`.

Identity-keyed pages — `/me/leave` and `/me/payslip`, which resolve their data from
`session['user_id']` via `_my_employee()` — therefore still showed the **admin's** data,
just rendered with the simulated role's chrome. The owner's words: "the data is about me
but in their role." Everything else in Sendy is role-gated only (same global data for any
user), so role-simulation was enough there but useless for the self-service surface.

The owner wanted to see **what a specific employee actually sees** — e.g. บอล's (general)
leave/payslip view, or a staff member's — to support the team and verify their experience.

## Decision

Turn "simulate a role" into **impersonate a user**: the admin temporarily *becomes a
specific user*.

- `admin_simulate_role` takes a `user_id` (not a role). It swaps `session['user_id'] +
  username + display_name + role` to the target, so identity-keyed pages show the target's
  data. The real identity is stashed in `_real_*` (set **once**, so user→user switches keep
  the original admin) and restored on exit.
- **Full act-as** (not view-only): while impersonating, any write is attributed to the
  target user (leave under their employee, `created_by` = their id). Chosen over view-only
  because it matches "see exactly what they see *and can do*" (e.g. confirm บอล *can*
  submit leave) and avoids a blanket POST-blocking gate. Guardrails: the persistent
  "โหมดจำลอง" banner (shows the impersonated person + role) and an `app.logger` "IMPERSONATE
  enter/exit" line recording the **real admin** behind the session.
- Any non-admin user can be impersonated (manager/staff/shareholder/general); admin targets
  are refused. Only a real admin can start.
- **Exit (and user-switch) are always reachable** regardless of the impersonated role: a
  `before_request` early-return allows `admin_exit_simulate` / `admin_simulate_role` when
  `_real_role` is set. Without it, impersonating `general` (every endpoint → stock-search)
  or `shareholder` (only logout may POST) would trap the admin with no way back.

The trail is an **app-logger line, not an `audit_log` row**: `audit_log.action` has a CHECK
constraint of `INSERT|UPDATE|DELETE` (it records row mutations), and impersonation is not a
row mutation. Extending the constraint would mean a table-rebuild migration on the
~441k-row `audit_log` — disproportionate for this.

## Consequences

- **Act-as is powerful and quiet.** An admin can become any user and write as them; to a
  reader of the data it looks like the target did it. The logger line is the only link back
  to the admin. Acceptable here: Sendy has exactly one admin (the owner), so this is a
  convenience for *himself*, not a multi-admin trust boundary. If a second admin is ever
  added, revisit (consider view-only, or DB-backed audit with an extended action set).
- Impersonation state lives entirely in the **signed-cookie session**, so it is coherent
  across Railway's `gunicorn -w 2` workers (no per-worker in-memory state).
- Impersonating a shareholder still shows "no leave system" for `/me/*` (shareholders are
  leave-exempt by design) and no-employee accounts show empty self-service — both correct.
- The per-user button lives on `/users` (admin-only), so in practice you exit before
  impersonating the next person; direct user→user switching is supported defensively.
