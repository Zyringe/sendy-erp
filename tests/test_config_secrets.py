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
    sys.modules.pop('config', None)
    return importlib.import_module('config')


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
