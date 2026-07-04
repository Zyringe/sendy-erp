"""Tests for config.py secret-required behavior.

The app must refuse to import config when SECRET_KEY or ADMIN_PASSWORD
is unset — no committed fallback defaults.
"""
import importlib
import os
import sys

import pytest


def _reload_config(monkeypatch, env):
    monkeypatch.setattr(os, 'environ', env)
    # Stub load_dotenv so a real local .env can't repopulate the monkey-patched
    # env mid-test — the assertion is about the *process* env, not about .env.
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: False)
    # Import a FRESH config for the assertion, but always restore the original
    # module object in sys.modules afterwards. Leaving a new (or no) 'config'
    # entry behind desyncs every module that bound `import config` at app-import
    # time from conftest's tmp_db monkeypatch target, which breaks later tests
    # (seen as test_upload_db_wal_safety operating on the real DATABASE_PATH).
    orig = sys.modules.pop('config', None)
    try:
        return importlib.import_module('config')
    finally:
        if orig is not None:
            sys.modules['config'] = orig
        else:
            sys.modules.pop('config', None)


def test_config_loads_when_secrets_present(monkeypatch):
    env = {'SECRET_KEY': 'test-key', 'ADMIN_PASSWORD': 'test-pwd'}
    cfg = _reload_config(monkeypatch, env)
    assert cfg.SECRET_KEY == 'test-key'
    assert cfg.ADMIN_PASSWORD == 'test-pwd'


def test_config_raises_when_secret_key_missing(monkeypatch):
    env = {'ADMIN_PASSWORD': 'test-pwd'}
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        _reload_config(monkeypatch, env)


def test_config_raises_when_admin_password_missing(monkeypatch):
    env = {'SECRET_KEY': 'test-key'}
    with pytest.raises(RuntimeError, match='ADMIN_PASSWORD'):
        _reload_config(monkeypatch, env)
