"""scripts/ must never sit AHEAD of inventory_app/ on sys.path (#476 option B).

name_builder, bsn_suggest and blueprints/bsn put scripts/ on sys.path so they
(and import_router's lazy `import import_express`) can reach helpers there.
With `insert(0, ...)` any scripts/<name>.py sharing a name with an
inventory_app module shadowed it — that is how every /call/<code> card 500'd
until #486 renamed the one colliding file. Appending closes the whole class.

Each entry point runs in a FRESH interpreter: in-process, whatever an earlier
test imported decides sys.path, so the answer would depend on collection order.
Removing scripts/ from sys.path entirely also passes — that is the fixed world.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

INVENTORY_APP = Path(__file__).resolve().parents[1] / 'inventory_app'

# '.' stands in for gunicorn's `--chdir inventory_app`.
_PROBE = """
import os, sys
sys.path.insert(0, '.')
{entry}
def first(target):
    return next((i for i, p in enumerate(sys.path)
                 if os.path.realpath(p or '.') == os.path.realpath(target)), -1)
print(first('.'), first('../scripts'))
"""


@pytest.mark.parametrize('entry', [
    'from app import app',   # the web app; runs name_builder's and blueprints/bsn's edits
    'import name_builder',
    'import bsn_suggest',    # a deferred import in the app, so it needs its own entry
])
def test_scripts_dir_never_precedes_inventory_app(entry, tmp_path):
    env = dict(os.environ, SKIP_DB_INIT='1', DATA_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, '-c', _PROBE.format(entry=entry)],
                          cwd=INVENTORY_APP, env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]

    inv, scripts = map(int, proc.stdout.split()[-2:])
    assert inv >= 0  # CONTROL: the probe found inventory_app/ at all
    assert scripts == -1 or inv < scripts, (
        f'{entry}: scripts/ at sys.path[{scripts}] is ahead of inventory_app/ at [{inv}]')
