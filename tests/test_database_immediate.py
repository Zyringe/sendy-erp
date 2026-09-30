"""Card F PR-1: `database.begin_immediate` / `database.immediate`, the one
transaction primitive every cashbook writer will run under (plan §3a, §3c).

`immediate(conn)` never owns the connection: it begins, then commits on a clean
exit or rolls back on an exception, and never closes. These tests run on a
plain temp SQLite file, because the primitive is table-agnostic.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database


def _db(tmp_path):
    path = str(tmp_path / 'imm.db')
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (v INTEGER)")
    c.commit()
    c.close()
    return path


def _count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


class _Flaky:
    """Wraps a real connection; `fail_on` names methods that raise instead."""

    def __init__(self, real, *fail_on):
        self._real = real
        self._fail_on = set(fail_on)
        self.closed = False

    def execute(self, *a, **k):
        return self._real.execute(*a, **k)

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def commit(self):
        if 'commit' in self._fail_on:
            raise sqlite3.OperationalError('disk I/O error (injected)')
        return self._real.commit()

    def rollback(self):
        if 'rollback' in self._fail_on:
            raise sqlite3.OperationalError('rollback failed (injected)')
        return self._real.rollback()

    def close(self):
        self.closed = True
        return self._real.close()


def test_begin_immediate_takes_the_write_lock_now(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        database.begin_immediate(conn)
        assert conn.in_transaction
        other = sqlite3.connect(path, timeout=0)
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute("INSERT INTO t VALUES (1)")
        other.close()
    finally:
        conn.rollback()
        conn.close()


def test_begin_immediate_refuses_a_transaction_in_flight(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO t VALUES (1)")        # implicit deferred txn
        assert conn.in_transaction
        with pytest.raises(database.CallerTransactionInFlight):
            database.begin_immediate(conn)
        assert conn.in_transaction, "the caller's transaction must be left alone"
        conn.commit()
        assert _count(path) == 1
    finally:
        conn.close()


def test_immediate_commits_on_a_clean_exit_and_never_closes(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        with database.immediate(conn) as c:
            assert c is conn
            assert conn.in_transaction
            conn.execute("INSERT INTO t VALUES (1)")
        assert not conn.in_transaction
        assert _count(path) == 1, "a fresh connection must see the committed row"
        assert conn.execute("SELECT 1").fetchone()[0] == 1, "immediate() closed the connection"
    finally:
        conn.close()


def test_immediate_rolls_back_and_reraises_on_exception(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(ValueError, match='boom'):
            with database.immediate(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise ValueError('boom')
        assert not conn.in_transaction, "the write lock must not outlive the with"
        assert _count(path) == 0
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_immediate_rolls_back_when_the_commit_itself_fails(tmp_path):
    path = _db(tmp_path)
    real = sqlite3.connect(path)
    conn = _Flaky(real, 'commit')
    try:
        with pytest.raises(sqlite3.OperationalError, match='disk I/O'):
            with database.immediate(conn):
                conn.execute("INSERT INTO t VALUES (1)")
        assert not real.in_transaction, "a failed commit must not leave the lock held"
        assert _count(path) == 0
        assert conn.closed is False
    finally:
        real.close()


def test_failed_commit_plus_failed_rollback_reports_both(tmp_path):
    path = _db(tmp_path)
    real = sqlite3.connect(path)
    conn = _Flaky(real, 'commit', 'rollback')
    try:
        with pytest.raises(database.ConnectionCleanupError) as caught:
            with database.immediate(conn):
                conn.execute("INSERT INTO t VALUES (1)")
        assert 'disk I/O' in str(caught.value.primary_error)
        assert 'rollback failed' in str(caught.value.cleanup_error)
        assert conn.closed is False
    finally:
        real.rollback()
        real.close()


def test_body_error_plus_failed_rollback_keeps_the_body_error_primary(tmp_path):
    path = _db(tmp_path)
    real = sqlite3.connect(path)
    conn = _Flaky(real, 'rollback')
    try:
        with pytest.raises(database.ConnectionCleanupError) as caught:
            with database.immediate(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise ValueError('boom')
        assert isinstance(caught.value.primary_error, ValueError)
        assert isinstance(caught.value.cleanup_error, sqlite3.OperationalError)
    finally:
        real.rollback()
        real.close()


def test_immediate_refuses_in_flight_and_leaves_the_caller_transaction(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(database.CallerTransactionInFlight):
            with database.immediate(conn):
                pytest.fail("the body must not run on a refused entry")
        assert conn.in_transaction, "a refused entry must not commit or roll back the caller"
        conn.rollback()
        assert _count(path) == 0
    finally:
        conn.close()


def test_immediate_looks_begin_immediate_up_at_call_time(tmp_path, monkeypatch):
    """Probe A (PR-2/PR-3) patches `database.begin_immediate`; that only works
    if `immediate()` resolves the name at call time."""
    path = _db(tmp_path)
    seen = []
    real = database.begin_immediate

    def probe(c):
        seen.append(True)
        return real(c)

    monkeypatch.setattr(database, 'begin_immediate', probe)
    conn = sqlite3.connect(path)
    try:
        with database.immediate(conn):
            pass
    finally:
        conn.close()
    assert seen == [True]


# ── hr keeps its names, now backed by database's primitives ────────────────

def test_hr_cleanup_error_is_the_database_class():
    import hr as hr_mod
    assert hr_mod.ConnectionCleanupError is database.ConnectionCleanupError


def test_hr_begin_immediate_delegates_and_keeps_payroll_wording(tmp_path, monkeypatch):
    import hr as hr_mod
    assert issubclass(hr_mod.CallerTransactionInFlight, database.CallerTransactionInFlight)
    path = _db(tmp_path)
    calls = []
    real = database.begin_immediate
    monkeypatch.setattr(database, 'begin_immediate', lambda c: (calls.append(c), real(c))[1])
    conn = sqlite3.connect(path)
    try:
        hr_mod._begin_immediate(conn)
        assert calls == [conn], "hr must take the lock through database.begin_immediate"
        assert conn.in_transaction
        with pytest.raises(hr_mod.CallerTransactionInFlight) as caught:
            hr_mod._begin_immediate(conn)
        assert 'ทำรายการเงินเดือนบน connection' in str(caught.value)
    finally:
        conn.rollback()
        conn.close()
