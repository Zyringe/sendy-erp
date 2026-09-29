"""Guards for scripts/2026_09_29_glove_pair_word.py — the glove `คู่` word + 1362's price.

SCOPE. These keep working after the script has run once and become history. The
evidence for the run itself is a rehearsal against a snapshot of prod, where all
eight products gained the word, no `last_paid` answer moved, and the six
customers of 1362 who were being quoted ฿0 started being quoted the book's ฿77.
"""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "2026_09_29_glove_pair_word.py"


def _load():
    spec = importlib.util.spec_from_file_location("glove_pair_word", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pair_ratio_is_one_because_a_glove_piece_is_already_a_pair():
    """Put's ruling, 2026-09-29: a glove's base unit is a PAIR, so `คู่` = 1 here.
    The tempting value is 2 — it is what 61/62 ขากลอน carry and what 1368 carried
    before #650 — and on these eight it would double the price of every pair
    quoted. The ratio is the whole ruling, so it gets its own guard."""
    mod = _load()
    src = _SCRIPT.read_text(encoding='utf-8')
    assert "(pid, PAIR_UNIT, 1.0)" in src, "the inserted ratio is no longer 1"
    assert mod.DOZEN_RATIO == 12.0


def test_only_1362_is_priced_and_it_has_no_price_today():
    """Everything else must arrive priced, or the script is quietly repricing."""
    mod = _load()
    assert mod.PLAN[mod.PRICED]['base'] == 0.0
    assert mod.PLAN[mod.PRICED]['tiers'] == []
    others = [pid for pid in mod.PLAN if pid != mod.PRICED]
    assert [pid for pid in others if mod.PLAN[pid]['base'] == 0.0] == [1358], \
        "1358 is the other unpriced glove and is deliberately NOT being priced here"


def test_the_base_is_the_printed_dozen_over_twelve_the_way_the_siblings_store_it():
    """1359/1360/1361 hold a rounded base plus a tier carrying the exact printed
    dozen. 1362 is the fourth of that set and must match, or its ฿0.04 of
    rounding drift reaches a quote instead of stopping at the tier."""
    mod = _load()
    assert mod.NEW_TIER == ('1 โหล', 77.0)
    assert mod.NEW_BASE == round(mod.NEW_TIER[1] / mod.DOZEN_RATIO, 2)
    assert mod.PLAN[1361]['base'] == mod.NEW_BASE, "1361 is the same 7 ขีด dozen at ฿77"


def test_this_is_not_registered_as_a_unit_rebase_source():
    """#613's list exempts a re-denomination from starting a price epoch. This
    script changes a real price (0 -> ฿77 a dozen), so it must not be on it.
    It needs no exemption anyway: #555 already treats a first-ever price as
    Sendy learning one rather than changing one."""
    import price_lookup
    assert _load().SOURCE not in price_lookup._UNIT_REBASE_SOURCES


def test_a_product_that_already_carries_the_pair_word_is_refused(empty_db_conn, monkeypatch):
    """The script's claim is that it supplies a MISSING word. If a `คู่` row is
    already there it means something else on that product, and writing a ratio-1
    row over it would change an answer rather than add one.

    `_ask` is stubbed because `price_lookup` reaches past the connection it is
    given — `_unit_word` calls `bsn_units.normalize_unit(...)` with no conn, so
    it opens the app's own DATABASE_PATH, which in a worktree has no unit_map.
    What is under test here is the structural gate; the resolver-level gate is
    what the rehearsal against a prod snapshot exercises."""
    mod = _load()
    c, pid = empty_db_conn, 1363
    plan = mod.PLAN[pid]
    monkeypatch.setattr(mod, '_ask', lambda conn, p, unit, today:
                        None if unit == mod.PAIR_UNIT else (mod._money(plan['dozen_lists']), 'tier'))
    c.execute("INSERT INTO products (id, product_name, unit_type, base_sell_price, cost_price) "
              "VALUES (?,?,?,?,0)", (pid, plan['label'], mod.OLD_UNIT, plan['base']))
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
              (pid, mod.DOZEN_UNIT, mod.DOZEN_RATIO))
    for label, price in plan['tiers']:
        c.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
                  (pid, label, price))
    assert not [p for p in mod.preconditions(c, [pid], '2026-09-29') if mod.PAIR_UNIT in p]

    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
              (pid, mod.PAIR_UNIT, 2.0))
    problems = mod.preconditions(c, [pid], '2026-09-29')
    assert [p for p in problems if mod.PAIR_UNIT in p], problems


def test_an_undeclared_product_table_refuses_the_run(empty_db_conn):
    """A table added to the schema later must not be silently skipped."""
    mod = _load()
    c = empty_db_conn
    c.execute("CREATE TABLE zz_new_feature (id INTEGER PRIMARY KEY, product_id INTEGER, note TEXT)")
    c.execute("INSERT INTO zz_new_feature (product_id, note) VALUES (1363, 'added later')")
    assert 'zz_new_feature' not in mod.TABLE_STORY
    problems = mod.preconditions(c, [1363], '2026-09-29')
    assert any('zz_new_feature' in p and 'TABLE_STORY' in p for p in problems), problems
