"""Optimiser tests. No database: the optimiser is pure Python."""

import itertools
import random
from collections import deque

import numpy as np
import pytest
from scipy.optimize import linprog

from domain.optimizer import InfeasibleRouteError, plan_fuel_stops

CAPACITY = 50.0
MPG = 10.0


def plan(positions, prices, distance, *, start_fuel=CAPACITY, stop_penalty=0.0):
    return plan_fuel_stops(
        positions,
        prices,
        distance,
        tank_capacity_gallons=CAPACITY,
        miles_per_gallon=MPG,
        start_fuel_gallons=start_fuel,
        stop_penalty=stop_penalty,
    )


def fuel_cost(purchases, prices):
    return sum(p.gallons * prices[p.index] for p in purchases)


def assert_physically_valid(purchases, positions, distance, start_fuel=CAPACITY):
    """Replay the plan: never run dry, never overfill, every stop buys something."""
    fuel, here = start_fuel, 0.0
    for purchase in purchases:
        fuel -= (positions[purchase.index] - here) / MPG
        assert fuel >= -1e-6, "ran out of fuel before a stop"
        assert fuel == pytest.approx(purchase.fuel_on_arrival, abs=1e-6)
        assert purchase.gallons > 1e-9
        fuel += purchase.gallons
        assert fuel <= CAPACITY + 1e-6, "tank overfilled"
        here = positions[purchase.index]
    assert fuel - (distance - here) / MPG >= -1e-6, "ran out of fuel before the destination"


# --- independent oracles -------------------------------------------------------------------


def refund_greedy_cost(positions, prices, distance, start_fuel=CAPACITY):
    """Classic exact algorithm for the zero-penalty problem (independent of the DP).

    Keep the tank as price-sorted lots. At each station "refund" every lot dearer
    than the local price and top up; always burn the cheapest lot first.
    """
    tank = deque([[0.0, start_fuel, False]]) if start_fuel > 0 else deque()
    level, here, spent = start_fuel, 0.0, 0.0
    for position, price in [*zip(positions, prices, strict=True), (distance, None)]:
        needed = (position - here) / MPG
        if needed > level + 1e-9:
            return None
        level -= needed
        while needed > 1e-12:
            lot = tank[0]
            used = min(lot[1], needed)
            lot[1] -= used
            needed -= used
            spent += used * lot[0]
            if lot[1] <= 1e-12:
                tank.popleft()
        here = position
        if price is None:
            break
        while tank and tank[-1][0] > price:
            level -= tank.pop()[1]
        if CAPACITY - level > 1e-12:
            tank.append([price, CAPACITY - level, True])
            level = CAPACITY
    return spent


def lp_cost(positions, prices, distance, start_fuel=CAPACITY):
    """Zero-penalty optimum as a linear programme (second independent oracle)."""
    n = len(positions)
    if n == 0:
        return 0.0 if distance <= start_fuel * MPG + 1e-9 else None
    rows, bounds = [], []
    lower = np.tril(np.ones((n, n)))
    used_on_arrival = np.array(positions) / MPG
    # Fuel on arrival at station i >= 0:  sum(buy[:i]) >= used_i - start
    arrival = np.tril(np.ones((n, n)), k=-1)
    rows.append(-arrival)
    bounds.append(start_fuel - used_on_arrival)
    # Fuel after buying at i <= capacity:  sum(buy[:i+1]) <= capacity - start + used_i
    rows.append(lower)
    bounds.append(CAPACITY - start_fuel + used_on_arrival)
    # Fuel at destination >= 0.
    rows.append(-np.ones((1, n)))
    bounds.append(np.array([start_fuel - distance / MPG]))
    result = linprog(prices, A_ub=np.vstack(rows), b_ub=np.concatenate(bounds), bounds=(0, None))
    return result.fun if result.status == 0 else None


def brute_force_objective(positions, prices, distance, start_fuel, stop_penalty):
    """Exhaustive optimum of fuel cost + penalty * stops over every subset of stations."""
    best = None
    indices = range(len(positions))
    for size in range(len(positions) + 1):
        for subset in itertools.combinations(indices, size):
            cost = lp_cost(
                [positions[i] for i in subset], [prices[i] for i in subset], distance, start_fuel
            )
            if cost is None:
                continue
            # Restricting to `subset` and paying for all of it is an upper bound that is
            # tight for the optimal subset, so the minimum over subsets is exact.
            value = cost + stop_penalty * size
            best = value if best is None else min(best, value)
    return best


def random_instance(rng, max_stations):
    distance = rng.uniform(50, 1800)
    count = rng.randint(0, max_stations)
    positions = sorted({round(rng.uniform(0.5, distance - 0.5), 1) for _ in range(count)})
    # Few distinct prices on purpose: ties are where the structure lemma is delicate.
    prices = [rng.choice([2.5, 2.75, 3.0, 3.0, 3.25, 3.999, 4.5]) for _ in positions]
    start_fuel = rng.choice([CAPACITY, CAPACITY, 30.0, 12.5, 0.0])
    return positions, prices, distance, start_fuel


# --- behaviour ------------------------------------------------------------------------------


def test_trip_within_starting_range_needs_no_stops():
    assert plan([100.0, 200.0], [3.0, 2.0], 480.0) == []


def test_single_required_stop_buys_exactly_the_shortfall():
    purchases = plan([400.0], [3.0], 700.0)
    assert [p.index for p in purchases] == [0]
    assert purchases[0].gallons == pytest.approx(20.0)  # 700 mi needs 70 gal, 50 on board
    assert purchases[0].fuel_on_arrival == pytest.approx(10.0)


def test_prefers_cheaper_station_and_fills_up_before_expensive_stretch():
    # Cheap fuel at mile 300, dear fuel at mile 600; 1,000-mile trip.
    purchases = plan([300.0, 600.0], [2.0, 4.0], 1000.0)
    assert_physically_valid(purchases, [300.0, 600.0], 1000.0)
    by_index = {p.index: p for p in purchases}
    assert by_index[0].fuel_on_departure == pytest.approx(CAPACITY)  # fill up where it is cheap
    assert by_index[1].gallons == pytest.approx(20.0)  # only the unavoidable remainder
    assert fuel_cost(purchases, [2.0, 4.0]) == pytest.approx(30 * 2.0 + 20 * 4.0)


def test_buys_just_enough_to_reach_a_cheaper_station():
    purchases = plan([400.0, 600.0], [4.0, 2.0], 1000.0)
    by_index = {p.index: p for p in purchases}
    assert by_index[0].gallons == pytest.approx(10.0)  # arrive at the cheap station empty
    assert by_index[1].fuel_on_arrival == pytest.approx(0.0)
    assert by_index[1].gallons == pytest.approx(40.0)


def test_stop_penalty_removes_marginal_stops():
    positions, prices, distance = [300.0, 320.0, 600.0], [2.99, 3.00, 3.50], 900.0

    # No penalty: fill up at mile 300, squeeze in 2 more gal at mile 320, 8 gal at mile 600.
    free = plan(positions, prices, distance, stop_penalty=0.0)
    assert [p.index for p in free] == [0, 1, 2]
    assert free[1].gallons == pytest.approx(2.0)
    assert fuel_cost(free, prices) == pytest.approx(30 * 2.99 + 2 * 3.00 + 8 * 3.50)

    # $2 per stop: skipping mile 300 costs 30 cents more fuel but saves a whole stop.
    penalised = plan(positions, prices, distance, stop_penalty=2.0)
    assert [p.index for p in penalised] == [1, 2]
    assert_physically_valid(penalised, positions, distance)
    assert fuel_cost(penalised, prices) == pytest.approx(32 * 3.00 + 8 * 3.50)


def test_empty_start_requires_a_station_at_the_origin():
    assert [p.index for p in plan([0.0], [3.0], 100.0, start_fuel=0.0)] == [0]
    with pytest.raises(InfeasibleRouteError):
        plan([10.0], [3.0], 100.0, start_fuel=0.0)


def test_infeasible_gap_is_reported():
    with pytest.raises(InfeasibleRouteError) as excinfo:
        plan([100.0, 700.0], [3.0, 3.0], 1500.0)
    error = excinfo.value
    assert (error.gap_start_miles, error.gap_end_miles) == (100.0, 700.0)
    assert error.reachable_miles == 500.0


def test_rejects_unsorted_or_duplicate_positions():
    with pytest.raises(ValueError):
        plan([200.0, 100.0], [3.0, 3.0], 900.0)
    with pytest.raises(ValueError):
        plan([200.0, 200.0], [3.0, 3.0], 900.0)


# --- optimality (property tests against independent oracles) -------------------------------


def test_zero_penalty_matches_refund_greedy_and_linear_programme():
    rng = random.Random(20261001)
    checked = 0
    for _ in range(1500):
        positions, prices, distance, start_fuel = random_instance(rng, max_stations=14)
        expected = refund_greedy_cost(positions, prices, distance, start_fuel)
        try:
            purchases = plan(positions, prices, distance, start_fuel=start_fuel)
        except InfeasibleRouteError:
            assert expected is None
            continue
        assert expected is not None
        assert_physically_valid(purchases, positions, distance, start_fuel)
        assert fuel_cost(purchases, prices) == pytest.approx(expected, abs=1e-6)
        if checked < 300:  # the LP is slower; a sample is plenty
            assert lp_cost(positions, prices, distance, start_fuel) == pytest.approx(
                expected, abs=1e-5
            )
        checked += 1
    assert checked > 500


def test_stop_penalty_matches_exhaustive_search():
    rng = random.Random(7)
    checked = 0
    for _ in range(400):
        positions, prices, distance, start_fuel = random_instance(rng, max_stations=7)
        penalty = rng.choice([0.25, 1.0, 2.0, 5.0, 25.0])
        expected = brute_force_objective(positions, prices, distance, start_fuel, penalty)
        try:
            purchases = plan(
                positions, prices, distance, start_fuel=start_fuel, stop_penalty=penalty
            )
        except InfeasibleRouteError:
            assert expected is None
            continue
        assert_physically_valid(purchases, positions, distance, start_fuel)
        objective = fuel_cost(purchases, prices) + penalty * len(purchases)
        assert objective == pytest.approx(expected, abs=1e-5)
        checked += 1
    assert checked > 150
