# inventory_app — Sendy ERP Flask application

Flask 3 / Python 3.9 / SQLite. No ORM. Single-process gunicorn on Railway.

## Quick start

```bash
# From sendy_erp/ root
~/.virtualenvs/erp/bin/pip install -r requirements-dev.txt
cp .env.example .env  # fill SECRET_KEY + ADMIN_PASSWORD (required since mig 069 PR)
~/.virtualenvs/erp/bin/python inventory_app/app.py
# or: sendy-up / sendy-down / sendy-log  (logs at /tmp/sendy.log)
```

App listens on `:5001`. DB at `instance/inventory.db`.

## Layout

| File | Responsibility |
|------|----------------|
| `app.py` | Flask app + most routes + POST whitelist (`_STAFF_POST_OK`, `_MANAGER_POST_OK`) |
| `models.py` | Business logic + raw-SQL queries |
| `database.py` | Schema bootstrap + migration runner (filename-keyed) |
| `config.py` | `DATABASE_PATH`, `SECRET_KEY`, `ADMIN_PASSWORD` (env-required since secrets sprint) |
| `parse_weekly.py` | BSN cp874 weekly file parser |
| `parse_cashbook.py` | NoVat cashbook Excel parser |
| `parse_platform.py` | Shopee / Lazada / TikTok parser |
| `bsn_suggest.py` | Smart-mapping suggester |
| `bsn_units.py` | BSN unit alias normalizer (mig 064) |
| `commission.py` | Commission engine (unit-aware, mig 063/064) |
| `payments_alloc.py` | FIFO payment-to-invoice allocator |
| `cashflow.py` | AR aging + cash-flow dashboard helpers |
| `revenue.py` | Revenue dashboard (Phase 3, by brand) |
| `ar_followup.py` | AR follow-up workspace + outreach log (mig 065) |
| `hr.py` / `hr_queries.py` | HR module reads/writes |
| `import_cashbook.py` | Cashbook Excel import (round-trip) |
| `import_credit_notes.py` | CN preview/confirm two-step (PR #36/#37) |
| `sku_code_utils.py` | SKU-code builder (post-mig 033 naming rule) |
| `blueprints/` | `bp_products`, `bp_cashbook`, `bp_hr`, `bp_supplier_catalogue`, `bp_mobile` |
| `imports/` | Express AR/AP parsers (Sendai Trading) |
| `templates/`, `static/` | Jinja + CSS |
| `instance/inventory.db` | SQLite live DB (NOT in git) |

## Authoritative references (do not duplicate)

- **Schema, routes, env, deploy:** `../CLAUDE.md` (app-level)
- **Workspace conventions, brand routing:** `../../CLAUDE.md` (workspace-level)
- **Permission model + POST whitelist:** `/erp-permissions` skill or `app.py:117-138`
- **Migrations history through 070:** `../CLAUDE.md` Migrations table

## Testing

```bash
cd sendy_erp && ~/.virtualenvs/erp/bin/pytest                          # full suite
~/.virtualenvs/erp/bin/pytest tests/test_cashflow.py                   # one file
~/.virtualenvs/erp/bin/pytest -k vat                                    # by keyword
```

`tests/conftest.py` provides:
- `tmp_db` fixture (copies live DB to a tmp_path + monkeypatches `config.DATABASE_PATH`) so tests never touch the live DB.
- Dummy `SECRET_KEY` / `ADMIN_PASSWORD` env via `os.environ.setdefault` (config now requires env, would otherwise fail at collection).
- `WTF_CSRF_ENABLED=False` env (unconditional) so existing POST tests don't need csrf_token rewrites.

## Adding a migration

1. Drop `data/migrations/NNN_name.sql` + `NNN_name.rollback.sql`
2. Restart the server (`sendy-down && sendy-up`)
3. Runner auto-applies on `init_db()`; sha256 + duration_ms recorded in `applied_migrations`
4. **Applied migrations are immutable** — fix bugs with a NEW higher-NNN migration, not by editing the old one (see `../CLAUDE.md` for the rare-exception rules)

For table-rebuild migrations (NOT NULL, type change, etc.), capture dependent VIEWs/TRIGGERs from `sqlite_master` and recreate them explicitly — DROP TABLE drops dependents silently. See `data/migrations/069_products_units_not_null.sql` for the working pattern (PRAGMA foreign_keys=OFF outside BEGIN, explicit INSERT…SELECT, dependent recreation).

## Adding a route

1. Decide blueprint vs `app.py`. New domain-coherent routes go in a blueprint under `blueprints/<name>.py` (see `bp_products` for the pattern).
2. If the route accepts POST, add its endpoint name to `_STAFF_POST_OK` or `_MANAGER_POST_OK` in `app.py`. The `tests/test_post_whitelist.py` typo-guard test will catch stale strings.
3. Restart the server (auto-reloader does NOT pick up new URL map entries reliably).
4. Add a test in `tests/test_<area>.py` (route-level via `app.test_client()` + session pre-population, or unit — see `tests/test_bp_products_routes.py` for the route-level pattern).

### CSRF protection

POST routes are CSRF-protected by default — no decorator required. All POST forms must include the hidden token input immediately after the opening `<form>` tag:

```html
<form method="post" action="...">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    ...
</form>
```

For AJAX / `fetch()` POSTs, read the token from the meta tag in `base.html` and send via `X-CSRFToken` header:

```js
fetch(url, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
  },
  body: JSON.stringify(payload)
})
```

Flask-WTF's `CSRFProtect(app)` rejects POSTs without a valid token with HTTP 400. The global `CSRFError` handler converts the 400 into a flash message + redirect to `request.referrer` (falling back to `/dashboard`).

Exemptions are explicit via `@csrf.exempt`. Currently exempt:
- `/bootstrap/upload-db` — gated by the `BOOTSTRAP_TOKEN` env var, has no logged-in session, no rendered form. Token is its own CSRF.
