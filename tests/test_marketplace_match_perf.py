"""Pins the cost model of ``_build_edges`` — the automatch hot loop.

2026-08-11 (why this file exists): the team's Shopee settlement import 500'd on
prod. Root cause was not a crash but COST: ``_build_edges`` scans every
(order, IV) pair, and every pair called ``_signed_gap``, which called
``datetime.strptime`` TWICE — 6.3M pairs → 12.7M strptime calls → ~50s of a
58s profile. Add the two ``_build_edges`` passes plus the bipartite match and
prod blew through gunicorn's ``--timeout 60``: WORKER TIMEOUT inside
``_min_cost_bipartite_match`` → the worker is SIGABRT'd → Internal Server
Error after a long wait (Railway log, /marketplace/upload, 14:17 + 14:19).

Two properties are pinned here:

  1. date parsing is O(distinct dates), NOT O(pairs) — the direct root cause;
  2. the date-bucketed candidate scan returns EXACTLY the same edges a naive
     all-pairs scan would, so the speedup can never silently change a match.

Both are deterministic (call counts / set equality), never wall-clock, so they
can't go flaky on a loaded CI box.
"""
import os
import random

os.environ.setdefault('SKIP_DB_INIT', '1')

import marketplace_match as mm


def _mk(n_orders, n_ivs, product_id=7):
    """A dense synthetic pool: every order/IV pair is date-valid and shares a
    product, so a naive scan touches all n_orders * n_ivs pairs."""
    orders = [{'order_sn': f'SN{i:04d}',
               'order_date': f'2026-07-{(i % 20) + 1:02d}',
               'billed_basis': 100.0 + i}
              for i in range(n_orders)]
    ivs = [{'doc_base': f'IV{j:04d}',
            'date_iso': f'2026-07-{(j % 20) + 1:02d}',
            'iv_net': 100.0 + j}
           for j in range(n_ivs)]
    o_prod = {o['order_sn']: {product_id} for o in orders}
    iv_prod = {iv['doc_base']: {product_id} for iv in ivs}
    return orders, ivs, iv_prod, o_prod


def _naive_edges(orders, ivs, iv_prod, o_prod, window_days):
    """Reference implementation: the plain all-pairs scan, using the module's
    own predicates. Any indexing trick in _build_edges must agree with this."""
    out = {}
    for o in orders:
        my_prod = o_prod.get(o['order_sn'], set())
        if not my_prod:
            continue
        payout = round(o['billed_basis'], 2)
        my_edges = []
        for iv in ivs:
            gap = mm._signed_gap(iv['date_iso'], o['order_date'])
            if gap is None or gap < 0 or gap > window_days:
                continue
            ivp = iv_prod.get(iv['doc_base'], set())
            if not mm._product_compatible(my_prod, ivp, payout, iv['iv_net']):
                continue
            my_edges.append((gap, abs((iv['iv_net'] or 0) - payout), iv['doc_base']))
        if my_edges:
            out[o['order_sn']] = my_edges
    return out


def test_build_edges_parses_each_date_once_not_once_per_pair(monkeypatch):
    """The root-cause pin: date parsing must scale with the number of DATES,
    not with the number of (order, IV) PAIRS.

    60x60 = 3,600 pairs. The pre-fix code parsed 2 dates per pair = 7,200
    strptime calls; the fixture only contains 20 distinct date strings.
    """
    orders, ivs, iv_prod, o_prod = _mk(60, 60)

    # No cache priming needed: a warm cache can only LOWER the count, and the
    # per-pair version parses 7,200 times whatever the cache state — so this
    # assertion cannot false-pass on a broken implementation.
    calls = []
    real_datetime = mm.datetime

    class _CountingDatetime:
        @staticmethod
        def strptime(value, fmt):
            calls.append(value)
            return real_datetime.strptime(value, fmt)

    monkeypatch.setattr(mm, 'datetime', _CountingDatetime)

    edges = mm._build_edges(orders, ivs, iv_prod, o_prod, mm.FORWARD_WINDOW_DAYS)

    # Guard against a vacuous pass: the loop really did produce candidate work.
    assert len(edges) == 60
    assert sum(len(v) for v in edges.values()) > 100
    # 20 distinct dates in the fixture. Allow slack for implementation detail,
    # but stay far below the 7,200 the per-pair parsing did.
    assert len(calls) <= 100, f'date parsed {len(calls)}x for 3,600 pairs'


def test_build_edges_matches_a_naive_all_pairs_scan():
    """Any date-window indexing must return exactly the naive scan's edges —
    same orders, same candidate sets, same (gap, adiff) costs."""
    orders, ivs, iv_prod, o_prod = _mk(40, 40)
    # Spread IV dates wider than the window so the index has to exclude some.
    for j, iv in enumerate(ivs):
        iv['date_iso'] = f'2026-07-{(j % 28) + 1:02d}'

    window = mm.FORWARD_WINDOW_DAYS
    got = mm._build_edges(orders, ivs, iv_prod, o_prod, window)
    want = _naive_edges(orders, ivs, iv_prod, o_prod, window)

    assert want, 'reference scan produced nothing — fixture is broken'
    assert set(got) == set(want)
    for sn in want:
        assert sorted(got[sn]) == sorted(want[sn]), sn


def test_build_edges_honours_the_forward_window_boundary():
    """An IV dated before the order, or past the window, is never a candidate —
    the property the date index must preserve at its edges."""
    orders = [{'order_sn': 'SN1', 'order_date': '2026-07-10', 'billed_basis': 100.0}]
    ivs = [
        {'doc_base': 'IV_BEFORE', 'date_iso': '2026-07-09', 'iv_net': 100.0},
        {'doc_base': 'IV_SAME', 'date_iso': '2026-07-10', 'iv_net': 100.0},
        {'doc_base': 'IV_LAST', 'date_iso': '2026-07-17', 'iv_net': 100.0},   # +7 = in
        {'doc_base': 'IV_PAST', 'date_iso': '2026-07-18', 'iv_net': 100.0},   # +8 = out
    ]
    o_prod = {'SN1': {7}}
    iv_prod = {iv['doc_base']: {7} for iv in ivs}

    edges = mm._build_edges(orders, ivs, iv_prod, o_prod, 7)

    assert sorted(db for _g, _a, db in edges['SN1']) == ['IV_LAST', 'IV_SAME']


def test_component_split_gives_the_same_assignment_as_one_big_solve():
    """The decomposition's whole safety argument: solving per connected
    component must equal solving the graph as ONE component.

    ``_match_one_component`` on the full edge set IS the pre-split monolithic
    solver, so it is an exact in-repo oracle — if the split ever changes an
    assignment (not just its cost), this goes red.
    """
    # Three clusters that must not interact, plus real competition inside two
    # of them (more orders than docs, so cardinality and cost both matter).
    edges = {
        'A1': [(0, 5.0, 'IVA'), (1, 0.0, 'IVB')],
        'A2': [(0, 1.0, 'IVA')],
        'A3': [(2, 0.0, 'IVB'), (0, 9.0, 'IVA')],
        'B1': [(1, 3.0, 'IVC')],
        'B2': [(0, 3.0, 'IVC'), (3, 1.0, 'IVD')],
        'C1': [(0, 0.0, 'IVE')],
    }

    split = mm._min_cost_bipartite_match(edges)
    monolithic = mm._match_one_component(edges)

    assert len(split) == 5, split          # control: real matching happened
    assert split == monolithic


def test_component_split_survives_tie_heavy_random_graphs():
    """The split's real risk is TIE-BREAKING, not cost: when two assignments
    cost the same, splitting could pick the other one and silently move an
    order to a different ใบกำกับ.

    Ties are not exotic here — ``_build_return_edges`` sets ``adiff = 0`` on
    every edge, so its whole cost space is gap-only and ties are the norm.
    So: 200 seeded random graphs, deliberately dense in ties (few distinct
    gaps, adiff always 0), each asserted equal to the monolithic solve.
    """
    rng = random.Random(20260811)
    compared = 0
    for _ in range(200):
        n_orders, n_docs = rng.randint(2, 14), rng.randint(2, 14)
        edges = {}
        for i in range(n_orders):
            picks = rng.sample(range(n_docs), rng.randint(1, min(4, n_docs)))
            # gap from a tiny domain + adiff pinned to 0 => maximum tie density,
            # exactly the returns-pass cost space.
            edges[f'SN{i}'] = [(rng.randint(0, 2), 0, f'IV{d}') for d in picks]

        split = mm._min_cost_bipartite_match(edges)
        monolithic = mm._match_one_component(edges)

        assert split == monolithic, (edges, split, monolithic)
        if split:
            compared += 1
    # Control: the graphs were not all degenerate/empty.
    assert compared > 150, f'only {compared} graphs produced a matching'


def test_candidate_components_separates_disjoint_clusters():
    """Two orders that share no doc_base must land in different components;
    two that chain through a shared doc must land in the same one."""
    edges = {
        'A1': [(0, 0.0, 'IV1')],
        'A2': [(0, 0.0, 'IV1'), (0, 0.0, 'IV2')],   # chains A1..A3 together
        'A3': [(0, 0.0, 'IV2')],
        'B1': [(0, 0.0, 'IV9')],                     # disjoint
    }
    comps = mm._candidate_components(edges)

    assert sorted(sorted(c) for c in comps) == [['A1', 'A2', 'A3'], ['B1']]


def test_signed_gap_still_returns_none_on_a_missing_date():
    """Cached parsing must not turn a missing date into an exception or a 0."""
    assert mm._signed_gap(None, '2026-07-10') is None
    assert mm._signed_gap('2026-07-10', None) is None
    assert mm._signed_gap('', '2026-07-10') is None
    assert mm._signed_gap('2026-07-12', '2026-07-10') == 2
    assert mm._signed_gap('2026-07-08', '2026-07-10') == -2
