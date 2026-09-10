"""After the app is imported, `import price_lookup` must reach the resolver.

Issue #476: three modules put `scripts/` at the FRONT of sys.path at import
time (name_builder, bsn_suggest, blueprints/bsn), and `scripts/` used to hold
a CLI wrapper also named `price_lookup.py`. `call_card._assemble_products`
does a bare `import price_lookup as pl` at request time, so it got the wrapper
and every `/call/<code>` 500'd with `no attribute 'epochs_for_pairs'` — on
local dev first, then on prod (2026-09-10).

Runs in a FRESH interpreter on purpose: inside pytest, whichever module an
earlier test file imported decides what `price_lookup` resolves to, so an
in-process check is green or red by collection order (measured in #476:
97 failures in a 57-file subset, 9 in the full suite, same code).
"""
import os
import subprocess
import sys
from pathlib import Path

INVENTORY_APP = Path(__file__).resolve().parents[1] / 'inventory_app'

# Mirrors how gunicorn loads the app (`--chdir inventory_app app:app`).
_PROBE = """
import sys
sys.path.insert(0, '.')
from app import app
import price_lookup
print(len(app.blueprints))
print(price_lookup.__file__)
print(hasattr(price_lookup, 'epochs_for_pairs'))
"""


def test_price_lookup_resolves_to_the_resolver_after_the_app_is_imported(tmp_path):
    # SECRET_KEY / ADMIN_PASSWORD come through from conftest's os.environ defaults.
    env = dict(os.environ,
               SKIP_DB_INIT='1',          # import only, never touch a DB
               DATA_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, '-c', _PROBE], cwd=INVENTORY_APP,
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]

    n_blueprints, resolved, has_epochs = proc.stdout.strip().splitlines()[-3:]
    # CONTROL: the app really was imported (a crash or an empty probe fails above).
    assert int(n_blueprints) >= 10
    assert Path(resolved).resolve() == INVENTORY_APP / 'price_lookup.py'
    assert has_epochs == 'True'
