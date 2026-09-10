"""scripts/price_lookup_cli.py — the one in-process test.

Every OTHER test for this script runs it as a subprocess on purpose (see
test_price_lookup_cli.py's docstring). This file deliberately does not, and
that is the point: it pins the mapping of an exception raised INSIDE
resolve_price, which — once the CLI validates its own inputs — can no longer
be provoked through stdin at all. There is no stdin payload that reaches it,
so a subprocess test of this behaviour is impossible to write.

Scoped-review finding 3: `except (ValueError, TypeError)` is broad enough to
swallow a genuine None-arithmetic bug in the resolver (which deliberately
carries None through unit.ratio / answer.qty / line_total / every internal.*
money key) and report it to Put as though he had typed bad input. Keeping the
batch alive is right; presenting an engine bug as a pricing answer is not.
"""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'price_lookup_cli.py'


def _load_script():
    missing = [k for k in ('SECRET_KEY', 'ADMIN_PASSWORD') if not os.environ.get(k)]
    if missing:
        pytest.skip(f"{' and '.join(missing)} not set — cannot import the CLI module")
    spec = importlib.util.spec_from_file_location('price_lookup_cli_script', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['price_lookup_cli_script'] = mod
    spec.loader.exec_module(mod)
    return mod


def test_engine_typeerror_is_labelled_as_internal_not_as_bad_input(monkeypatch):
    """A TypeError from inside resolve_price is a BUG, not bad input — say so.
    conn is None because the raise happens before any DB access on this path
    (an explicit product_id short-circuits both lookup helpers)."""
    mod = _load_script()

    def _boom(*a, **kw):
        raise TypeError("unsupported operand type(s) for *: 'NoneType' and 'float'")

    monkeypatch.setattr(mod.pl, 'resolve_price', _boom)
    out = mod._resolve_line(None, {'product_id': 26}, None)

    assert 'error' in out and 'result' not in out
    assert out['error'].startswith('internal error'), out['error']
    assert "NoneType" in out['error']


def test_engine_valueerror_stays_a_plain_user_facing_message(monkeypatch):
    """CONTROL for the test above: a ValueError is the resolver's normal way
    of saying 'this ask cannot be priced' (unknown unit, product not found),
    and must NOT pick up the internal-error label — otherwise the label means
    nothing."""
    mod = _load_script()

    def _boom(*a, **kw):
        raise ValueError("product_id 999999 not found")

    monkeypatch.setattr(mod.pl, 'resolve_price', _boom)
    out = mod._resolve_line(None, {'product_id': 999999}, None)

    assert out['error'] == "product_id 999999 not found"


def test_an_unanticipated_exception_type_does_not_kill_the_batch(monkeypatch):
    """The review of this very change: validating input types is still an
    ENUMERATION, and the evidence that enumerations miss shapes is that the
    first pass missed sqlite3.InterfaceError and AttributeError. A net under
    the enumeration is the only version whose guarantee does not depend on
    having imagined every shape — an unanticipated exception degrades to a
    per-line internal error, never a dead batch.

    sqlite3.InterfaceError is the real one that got through: it is neither
    ValueError nor TypeError."""
    mod = _load_script()

    def _boom(*a, **kw):
        raise sqlite3.InterfaceError(
            "Error binding parameter 0 - probably unsupported type.")

    monkeypatch.setattr(mod.pl, 'resolve_price', _boom)
    out = mod._resolve_line(None, {'product_id': 26}, None)

    assert 'error' in out and 'result' not in out
    assert out['error'].startswith('internal error'), out['error']
    assert 'binding parameter' in out['error']
