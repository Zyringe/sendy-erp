"""Guards for scripts/2026_09_29_rebase_gloves_650.py — #650, ถุงมือหนังขุย.

SCOPE. These keep working after the script has run once and become history. The
evidence for the run itself is a rehearsal of the real script against a snapshot
forced to match prod row for row, where the cost the WACC rebuild lands on
(33.25 / 28.50) is a prediction that only comes true if a โหล really is twelve
pairs, and all 23 repeat-customer quotes held.
"""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_29_rebase_gloves_650.py"
SOURCE = 'script:2026_09_29_rebase_gloves_650'


def _load():
    spec = importlib.util.spec_from_file_location("rebase_gloves_650", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_source_is_registered_with_the_resolver():
    """#613: base_sell_price doubles here, and an unattributed base change is a new
    price epoch — 23 repeat customers would drop from last_paid to list."""
    import price_lookup
    assert _load().SOURCE == SOURCE
    assert SOURCE in price_lookup._UNIT_REBASE_SOURCES


def test_the_pair_price_is_twice_the_old_piece_price():
    """The rebase re-denominates, it does not reprice: one pair is two gloves."""
    mod = _load()
    for pid, plan in mod.PLAN.items():
        assert plan['pair_price'] == plan['old_base'] * 2, pid
        assert plan['preserve'] == 0, pid


def test_1368_carries_no_price_so_the_rebase_cannot_move_one():
    """1368 is the same defect with the price left out, and that is the whole
    reason it is cheap. `x == x * 2` holds vacuously at zero, so the rule above
    says nothing about this product — state the zero outright, or a later edit
    could give 1368 a price here and the doubling rule would wave it through."""
    plan = _load().PLAN[1368]
    assert plan['old_base'] == 0.0 and plan['pair_price'] == 0.0
    assert plan['drop_tier'] is False, "1368 has no tier; demanding one refuses the run"


def test_the_source_is_set_per_product_not_once_around_the_loop(monkeypatch):
    """models/wacc.py sets the source to 'wac-sync' for its own writes, so the
    first product's cost rebuild leaves the SECOND product's base change
    unattributed. Caught in rehearsal: 1364's epoch held and 1365's moved."""
    src = _SCRIPT.read_text(encoding='utf-8')
    body = src.split('def rebase_one', 1)[1].split('\ndef ', 1)[0]
    assert '_set_price_change_source(conn, SOURCE)' in body, \
        "the per-product source stamp is gone; 1365-shaped products lose their attribution"


def test_an_undeclared_product_table_refuses_the_run(empty_db_conn):
    """A table added to the schema later must not be silently skipped."""
    mod = _load()
    c = empty_db_conn
    c.execute("CREATE TABLE zz_new_feature (id INTEGER PRIMARY KEY, product_id INTEGER, note TEXT)")
    c.execute("INSERT INTO zz_new_feature (product_id, note) VALUES (1364, 'added later')")
    assert 'zz_new_feature' not in mod.TABLE_STORY

    class _Eng:
        OPENING_NOTE = RECONCILE_NOTE = OPENING_STAMP = ''

    problems = mod.preconditions(c, _Eng, [1364])
    assert any('zz_new_feature' in p and 'TABLE_STORY' in p for p in problems), problems


def test_the_tier_expectation_is_per_product(empty_db_conn):
    """1364/1365 each had to LOSE a `1 คู่` tier; 1368 has none to lose. When that
    expectation was a module constant the run refused 1368 for a tier that was
    never there, so the branch has to be driven from the plan — in both
    directions, or the guard is only ever asserting the shape it was written for."""
    mod = _load()
    c, plan = empty_db_conn, mod.PLAN[1368]
    c.execute("INSERT INTO products (id, product_name, unit_type, base_sell_price, cost_price) "
              "VALUES (?,?,?,?,?)", (1368, plan['label'], mod.OLD_UNIT, plan['old_base'], plan['cost']))
    for unit, ratio in ((mod.NEW_UNIT, 2.0), (mod.DOZEN_UNIT, mod.DOZEN_RATIO)):
        c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                  (1368, unit, ratio))

    class _Eng:
        OPENING_NOTE = RECONCILE_NOTE = OPENING_STAMP = ''

    assert not [p for p in mod.preconditions(c, _Eng, [1368]) if 'tier' in p]

    c.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
              (1368, '1 โหล', 500.0))
    assert [p for p in mod.preconditions(c, _Eng, [1368]) if 'tier' in p]


def test_the_dozen_stops_quoting_half_price():
    """The live commercial error this fixes: a โหล listed ฿300 for ฿600 of gloves.
    The invariant asserts pair_price x 12, so it cannot pass while the ratio is wrong."""
    src = _SCRIPT.read_text(encoding='utf-8')
    assert "plan['pair_price'] * DOZEN_RATIO" in src
    mod = _load()
    assert mod.PLAN[1364]['pair_price'] * mod.DOZEN_RATIO == 600.0
    assert mod.PLAN[1365]['pair_price'] * mod.DOZEN_RATIO == 504.0
