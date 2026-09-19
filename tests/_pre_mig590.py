"""Run a DATED one-off script's test in the world that script ran in (#590).

Migration 190 refuses a cost write unless someone is declared, and a raw
`sqlite3.connect()` cannot even compile one (`no such function: sendy_actor`).
That is the point, and every LIVE writer now declares: merge_product.py and
remap_bsn_code.py were FIXED (script_connection), not exempted. But some test
files drive dated one-off scripts whose docstrings say never to re-run them.
They abort if re-run, and that is ACCEPTED. Their tests exist to document what
those runs did, so they must run in a world where the script's own raw
connections can write.

Dropping the triggers would not be enough: the WACC engine also refuses a
connection that names nobody (actor.require), before its first write. So this
registers `sendy_actor()` on every connection the test opens. With no identity
bound, it resolves the per-test default from conftest (`pytest`), and the
triggers and the preflight both stay live and are both exercised.

⛔ THIS IS NOT A WAY TO SILENCE THE GUARD. `test_pre_mig590_usage.py` fails if a
file outside its recorded list calls this.
"""
import sqlite3

import actor

_real_connect = sqlite3.connect


def sign_raw_connections(monkeypatch):
    def _connect(*args, **kwargs):
        return actor.install(_real_connect(*args, **kwargs))
    monkeypatch.setattr(sqlite3, 'connect', _connect)
