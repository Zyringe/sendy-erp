"""Card E (ADR 0020): `scripts/import_express.py sales` is no longer a CLI choice."""
import os
import sys

import pytest

# scripts/ goes on sys.path LAST (a same-named script must never shadow an app module).
_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)


def _run_cli(monkeypatch, capsys, *argv):
    import import_express
    monkeypatch.setattr(sys, 'argv', ['import_express.py', *argv])
    # Stop after argument parsing: reaching run_import means argparse accepted it.
    monkeypatch.setattr(import_express, 'run_import',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('reached run_import')))
    with pytest.raises((SystemExit, RuntimeError)) as exc:
        import_express.main()
    return exc.value, capsys.readouterr().err


def test_sales_is_not_a_cli_choice(monkeypatch, capsys):
    exc, err = _run_cli(monkeypatch, capsys, 'sales', 'x.csv')
    assert isinstance(exc, SystemExit) and exc.code == 2
    assert 'invalid choice' in err


def test_a_live_choice_still_gets_past_argparse(monkeypatch, capsys):
    """CONTROL: the same call shape with a live type reaches run_import."""
    exc, err = _run_cli(monkeypatch, capsys, 'payments_in', 'x.csv')
    assert isinstance(exc, RuntimeError) and 'reached run_import' in str(exc)
    assert 'invalid choice' not in err


def test_the_importer_for_express_sales_is_kept():
    import import_express
    assert 'sales' in import_express._IMPORTERS
