"""Guards for scripts/2026_09_28_rebase_dozen_649.py — #649, five โหล products.

SCOPE, stated plainly. These are the checks that keep working after the script
has run once and become history. They are NOT a substitute for the run's own
evidence, which is a rehearsal of the real script against a snapshot forced to
match prod row for row, plus a negative control with the allowlist line removed
that showed five real repeat customers jumping from last_paid to list. Unlike
`test_rebase_gross_603.py`, this file does not rebuild the five products as a
fixture; the script's own preconditions refuse anything that is not the measured
pre-state, and its invariants roll back inside the transaction.
"""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_28_rebase_dozen_649.py"
SOURCE = 'script:2026_09_28_rebase_dozen_649'


def _load():
    spec = importlib.util.spec_from_file_location("rebase_dozen_649", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_source_is_registered_with_the_resolver():
    """#613: a rebase SOURCE missing from this tuple resets every repeat customer to list.

    Measured on the #649 set: without it, 34ร10 is quoted ฿700 instead of the
    ฿630 it last paid, 43ท013 ฿800 instead of ฿720, and three more the same way.
    """
    import price_lookup
    assert _load().SOURCE == SOURCE
    assert SOURCE in price_lookup._UNIT_REBASE_SOURCES


def test_base_price_divides_the_catalogue_exactly():
    """The reason this script does not round the sell price up to 2 dp.

    603 rounds up, a convention from the 2026-08-17 batch. Here the target shape
    is siblings 1865/1866, whose bases the catalogue import left unrounded, and
    exact division is what makes `base x 12 == the printed โหล price` checkable.
    """
    mod = _load()
    for pid, plan in mod.PLAN.items():
        assert plan['new_base'] * mod.RATIO == plan['dozen'], pid
        assert plan['preserve'] == plan['stock'] * mod.RATIO, pid


def test_an_undeclared_product_table_refuses_the_run(empty_db_conn, tmp_path):
    """A table added to the schema later must not be silently skipped.

    The schema carries ~20 product-keyed tables that hold nothing for these five,
    and listing them would be noise that rots. The script measures instead: any
    product-keyed table outside TABLE_STORY that actually holds a row for a pid
    being rebased refuses the run. This drives that path with a table invented
    here, which is the only way to know the scan can produce a hit at all.
    """
    mod = _load()
    c = empty_db_conn
    c.execute("CREATE TABLE zz_new_feature (id INTEGER PRIMARY KEY, product_id INTEGER, note TEXT)")
    c.execute("INSERT INTO zz_new_feature (product_id, note) VALUES (831, 'added later')")
    assert 'zz_new_feature' not in mod.TABLE_STORY

    class _Eng:  # the scan runs before any engine call
        _stock = _ledger_sum = staticmethod(lambda *a, **k: 0)

    problems = mod.preconditions(c, _Eng, [831])
    assert any('zz_new_feature' in p and 'TABLE_STORY' in p for p in problems), problems


def test_only_a_percent_promotion_is_allowed_through():
    """831 carries an active promo, which the engine's own MUST_BE_EMPTY would
    refuse. A percentage is ratio-invariant; a fixed amount would silently become
    12x its intended discount, so only 'percent' may pass."""
    src = _SCRIPT.read_text(encoding='utf-8')
    assert "!= 'percent'" in src
    assert 'promotions' not in {t for t, _ in _load().MUST_BE_EMPTY}
