"""`--preload` must stay on the gunicorn start command.

Why this is a test and not a comment
------------------------------------
Railway runs `gunicorn -w 2`. Without `--preload`, EACH worker imports
`inventory_app/app.py` independently, and that module calls `init_db()` at
import time (app.py, `with app.app_context(): init_db()`). So the migration
runner runs once per worker and the two race:

    worker A: [migration] applied 143_....sql in 9ms      <- applies + stamps
    worker B: [migration] FAILED 143_....sql: table ... already exists
              [ERROR] Reason: Worker failed to boot.
              [ERROR] Shutting down: Master

Reproduced locally against a copy of the live DB with the migration forced back
to pending — identical failure. Re-run with `--preload` on the same state: the
migration runs exactly once, both workers boot, `/healthz` 200.

It self-heals: Railway restarts (`restartPolicyType = "on_failure"`), and by
then the migration is stamped so both workers skip it. That is why this has
almost certainly been happening on every migration deploy for months without
anyone noticing — an `ALTER TABLE ADD COLUMN` loses the race with "duplicate
column name" just the same. The cost is a boot crash per deploy, and the tail
risk is a migration slow enough to exhaust `healthcheckTimeout`, or one that
does partial damage before the loser fails.

Removing `--preload` silently reintroduces all of it, and the symptom appears
only in prod, only during a migration deploy, and disappears on retry. Hence a
test.

Fork-safety (checked 2026-07-30, keep true if you add import-time work):
  - `init_db()` closes its connection (`database.py`), and importing `app`
    leaves ZERO live `sqlite3.Connection` objects — nothing DB-ish is inherited
    across the fork.
  - No module-level mutable caches exist to be shared at fork; the codebase
    already bans them (see `.claude/rules/erp-engineering-discipline.md`, the
    `_OVERRIDES_CACHE` cross-worker money bug).
  A genuine migration failure is still fully visible under `--preload`
  (`[migration] FAILED` + traceback) and the app still refuses to serve.
"""
from __future__ import annotations

import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROCFILE = os.path.join(REPO, "Procfile")
RAILWAY_TOML = os.path.join(REPO, "railway.toml")

_REASON = (
    "gunicorn must run with --preload, or each of the 2 workers imports app.py "
    "and runs init_db() -> both race the migration runner -> the loser raises on "
    "already-applied DDL and takes the master down. See this module's docstring."
)


def _procfile_web_command():
    for line in open(PROCFILE, encoding="utf-8").read().splitlines():
        if line.strip().startswith("web:"):
            return line.split("web:", 1)[1].strip()
    raise AssertionError("no `web:` line in Procfile")


def _railway_start_command():
    src = open(RAILWAY_TOML, encoding="utf-8").read()
    # Ignore commented-out lines so the rationale comment above the setting
    # cannot satisfy this by accident.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r'startCommand\s*=\s*"(.*)"\s*$', stripped)
        if m:
            return m.group(1)
    raise AssertionError("no startCommand in railway.toml")


def test_procfile_has_preload():
    cmd = _procfile_web_command()
    assert "--preload" in cmd, f"Procfile: {_REASON}\n  got: {cmd}"


def test_railway_toml_has_preload():
    cmd = _railway_start_command()
    assert "--preload" in cmd, f"railway.toml: {_REASON}\n  got: {cmd}"


def test_procfile_and_railway_toml_agree():
    """The start command is duplicated in two files. Railway uses
    railway.toml's `startCommand`; the Procfile is the fallback and what a
    local/other-PaaS run would follow. They drifting apart is how one of them
    silently loses --preload."""
    proc = _procfile_web_command()
    rail = _railway_start_command()
    assert proc == rail, (
        "Procfile and railway.toml start commands have drifted:\n"
        f"  Procfile     : {proc}\n"
        f"  railway.toml : {rail}"
    )


def test_preload_only_matters_because_init_db_runs_at_import():
    """Pins the precondition. If `init_db()` ever stops running at import time
    (e.g. moved behind a CLI entrypoint or a Flask CLI command), the --preload
    requirement above becomes obsolete rather than load-bearing, and this test
    is the breadcrumb that says so."""
    src = open(os.path.join(REPO, "inventory_app", "app.py"), encoding="utf-8").read()
    # Indented (so: executed at import, not merely defined), any depth — it
    # currently sits inside the SKIP_DB_INIT guard within `with app.app_context()`.
    assert re.search(r"^[ \t]+init_db\(\)\s*$", src, re.M), (
        "init_db() no longer appears to run at app.py import time. If that is "
        "deliberate, re-evaluate whether --preload is still required and update "
        "this module's docstring."
    )
