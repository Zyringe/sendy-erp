"""import_weekly must not leak its connection when the import raises.

`import_weekly` opens a connection and then runs ~260 lines — the per-entry
loop, removal detection, the pass-2 ledger rebuild, the new-code registration —
before its first `commit()`. Until this test existed, none of that was inside a
`try`/`finally`, so a raise anywhere in it returned to the caller with the
connection still open and holding a write transaction.

Its read-only twin `preview_import` DOES have `try: ... finally: conn.close()`.
Two functions over the same table with opposite failure contracts, neither
saying so, is the shape this pins.

The trigger is not hypothetical: the `entries` shape is undeclared, has two
independent producers (`parse_weekly` and `express_dbf_source.build_*_entries`),
and is read with bare subscripts, so a key one producer forgets raises
mid-loop after N rows have already been inserted.
"""
import sqlite3

import pytest


class _Boom(Exception):
    """Sentinel: stands in for any raise inside the pre-commit stretch."""


class _TrackedConnection:
    """Delegates to a real sqlite3 connection, but records close()/commit().

    A real connection is used rather than a mock so the code under test does
    genuine SQL right up to the raise — the leak only matters because there is
    an open write transaction behind it.
    """

    def __init__(self, real):
        self._real = real
        self.close_calls = 0
        self.commit_calls = 0

    def close(self):
        self.close_calls += 1
        return self._real.close()

    def commit(self):
        self.commit_calls += 1
        return self._real.commit()

    def in_transaction(self):
        return self._real.in_transaction

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def tracked(empty_db, monkeypatch):
    """Hand import_weekly a connection we can watch it fail to close."""
    from models import imports as imports_mod

    made = []

    def _fake_get_connection():
        real = sqlite3.connect(empty_db)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        tc = _TrackedConnection(real)
        made.append(tc)
        return tc

    monkeypatch.setattr(imports_mod, 'get_connection', _fake_get_connection)
    return made


def _entries():
    return [{
        'date_iso': '2026-03-04', 'doc_no': 'IV6900001-1', 'product_code_raw': 'X1',
        'product_name_raw': 'ของทดสอบ', 'party': 'ลูกค้า ก', 'party_code': 'C1',
        'qty': 1.0, 'unit': 'ตัว', 'unit_price': 10.0, 'vat_type': 0,
        'discount': 0.0, 'total': 10.0, 'net': 10.0, 'line_seq': 1,
    }]


def test_a_raise_before_the_commit_still_closes_the_connection(tracked, monkeypatch):
    """The leak. Fails on any build where the pre-commit stretch has no finally."""
    from models import imports as imports_mod

    def _explode(*a, **k):
        raise _Boom('something in the pre-commit stretch went wrong')

    monkeypatch.setattr(imports_mod, '_resolve_mapping', _explode)

    with pytest.raises(_Boom):
        imports_mod.import_weekly(_entries(), 'sales', 'x.csv')

    # CONTROL: the function really did get as far as opening a connection, so
    # a passing assertion below means "closed", not "never opened".
    assert len(tracked) == 1, (
        'import_weekly did not open a connection at all — the test never '
        'reached the code it claims to check')
    assert tracked[0].commit_calls == 0, 'nothing should have been committed'
    assert tracked[0].close_calls == 1, (
        'import_weekly returned through an exception with its connection still '
        'open, holding a write transaction')


def test_the_happy_path_still_closes_exactly_once(tracked):
    """A guard must survive its own success: the fix must not double-close."""
    from models import imports as imports_mod

    imports_mod.import_weekly(_entries(), 'sales', 'x.csv')

    assert len(tracked) == 1
    assert tracked[0].close_calls == 1, (
        'the connection must be closed exactly once on the happy path — a '
        'finally that fires after an explicit close would double it')
    assert tracked[0].commit_calls >= 1, 'the happy path must commit'


def test_preview_import_already_had_this_contract(empty_db, monkeypatch):
    """CONTROL for the whole file: the twin function is the shape being copied.

    If this ever fails, the asymmetry moved rather than being fixed.
    """
    from models import imports as imports_mod
    import inspect

    src = inspect.getsource(imports_mod.preview_import)
    assert 'finally:' in src and 'conn.close()' in src, (
        'preview_import lost its try/finally — the contract this file pins '
        'import_weekly against no longer exists')
