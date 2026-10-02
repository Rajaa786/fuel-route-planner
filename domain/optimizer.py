"""Exact fuel-stop optimiser.

Pure Python with no Django (or numpy) imports: the algorithm is the heart of the
service and must stay independently testable.

Problem
-------
A vehicle drives a fixed route of ``total_distance`` miles. Candidate stations
sit at known mile markers with known prices. The tank holds
``tank_capacity_gallons``; the vehicle starts with ``start_fuel_gallons``.
Choose where to stop and how much to buy so as to minimise::

    sum(price_i * gallons_i)  +  stop_penalty * number_of_stops

With ``stop_penalty == 0`` this is the textbook cost-optimal plan, which in
practice asks the driver to pull over for a gallon or two whenever the next
town is a cent cheaper. A small penalty prices in the time a stop costs and
removes those stops; the result is still an exact optimum of the stated
objective, not a heuristic.

Method
------
Structure lemma ("To Fill or Not to Fill", Khuller, Malekian & Mestre): in an
optimal plan, for consecutive stops ``i -> j``

* if ``price_i < price_j``: leave ``i`` with a **full tank**;
* otherwise: buy **just enough** at ``i`` to reach ``j`` with an empty tank.

So the fuel level on arrival at any stop is either 0 or "full tank minus the
distance from a cheaper predecessor" (or what is left of the starting fuel).
That gives a DP over ``(station, arrival_fuel)`` states. Two observations keep
it fast:

* the cost of leaving ``i`` full does not depend on the successor, so it is
  computed once per station;
* the best "arrive at ``j`` empty" transition is a prefix minimum over ``i``'s
  states ordered by arrival fuel, found with one bisect.

Complexity is ``O(n * w * log w)`` where ``w`` is the number of stations within
one tank of range: about a millisecond for a coast-to-coast route.
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

# Tolerance (gallons / miles) for float comparisons; far below a cent of fuel.
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Purchase:
    """One planned stop: index into the candidate arrays and the fuel bought."""

    index: int
    gallons: float
    fuel_on_arrival: float

    @property
    def fuel_on_departure(self) -> float:
        return self.fuel_on_arrival + self.gallons


class InfeasibleRouteError(Exception):
    """No plan exists: some stretch of the route exceeds the reachable range."""

    def __init__(self, gap_start_miles: float, gap_end_miles: float, reachable_miles: float):
        self.gap_start_miles = gap_start_miles
        self.gap_end_miles = gap_end_miles
        self.reachable_miles = reachable_miles
        super().__init__(
            f"No fuel station between mile {gap_start_miles:.1f} and mile {gap_end_miles:.1f} "
            f"({gap_end_miles - gap_start_miles:.1f} mi) but only {reachable_miles:.1f} mi "
            "of range is available there."
        )


def plan_fuel_stops(
    positions: Sequence[float],
    prices: Sequence[float],
    total_distance: float,
    *,
    tank_capacity_gallons: float,
    miles_per_gallon: float,
    start_fuel_gallons: float,
    stop_penalty: float = 0.0,
) -> list[Purchase]:
    """Return the optimal purchases, in route order.

    ``positions`` must be strictly increasing mile markers within
    ``[0, total_distance]``; ``prices`` are dollars per gallon. Raises
    :class:`InfeasibleRouteError` if the destination cannot be reached.
    """
    count = len(positions)
    if len(prices) != count:
        raise ValueError("positions and prices must have the same length.")
    if any(positions[i] >= positions[i + 1] for i in range(count - 1)):
        raise ValueError("positions must be strictly increasing.")
    if not 0 <= start_fuel_gallons <= tank_capacity_gallons:
        raise ValueError("start_fuel_gallons must be within the tank capacity.")
    if stop_penalty < 0:
        # A negative penalty would reward pointless stops; the DP assumes it never pays to
        # stop without buying.
        raise ValueError("stop_penalty must not be negative.")

    capacity = tank_capacity_gallons
    mpg = miles_per_gallon
    full_range = capacity * mpg
    start_range = start_fuel_gallons * mpg

    if total_distance <= start_range + _EPSILON:
        return []

    # states[i]: (arrival_fuel, cost_so_far, back_pointer) for "we stop at i".
    # cost_so_far excludes the purchase (and penalty) at i itself.
    # back_pointer is None (came from the origin) or (prev_station, prev_state, left_full).
    states: list[list[tuple[float, float, tuple[int, int, bool] | None]]] = [
        [] for _ in range(count)
    ]
    for j in range(count):
        if positions[j] > start_range + _EPSILON:
            break
        states[j].append((start_fuel_gallons - positions[j] / mpg, 0.0, None))

    # Best known way to arrive at each station with an empty tank.
    arrive_empty: list[tuple[float, tuple[int, int, bool]] | None] = [None] * count
    best_total = float("inf")
    best_final: tuple[int, int] | None = None

    for i in range(count):
        if arrive_empty[i] is not None:
            cost, back = arrive_empty[i]
            states[i].append((0.0, cost, back))
        if not states[i]:
            continue

        states[i].sort(key=lambda state: state[0])
        fuels = [state[0] for state in states[i]]
        price = prices[i]
        position = positions[i]

        # prefix[k] = min over states[:k + 1] of (cost - price * arrival_fuel), with its argmin.
        # Buying up to a level L from state s costs price * (L - fuel_s), so the best state
        # for any target level is this prefix minimum over the states that arrive below L.
        prefix: list[tuple[float, int]] = []
        running, running_arg = float("inf"), -1
        for k, (fuel, cost, _) in enumerate(states[i]):
            value = cost - price * fuel
            if value < running:
                running, running_arg = value, k
            prefix.append((running, running_arg))

        leave_full = _cheapest_fill(fuels, prefix, price, capacity, stop_penalty)

        j = i + 1
        while j < count and positions[j] - position <= full_range + _EPSILON:
            needed = (positions[j] - position) / mpg
            if prices[j] > price:
                if leave_full is not None:
                    states[j].append((capacity - needed, leave_full[0], (i, leave_full[1], True)))
            else:
                option = _cheapest_fill(fuels, prefix, price, needed, stop_penalty)
                if option is not None and (
                    arrive_empty[j] is None or option[0] < arrive_empty[j][0]
                ):
                    arrive_empty[j] = (option[0], (i, option[1], False))
            j += 1

        if total_distance - position <= full_range + _EPSILON:
            final_leg = (total_distance - position) / mpg
            option = _cheapest_fill(fuels, prefix, price, final_leg, stop_penalty)
            if option is not None and option[0] < best_total:
                best_total, best_final = option[0], (i, option[1])

    if best_final is None:
        raise _diagnose_gap(positions, total_distance, start_range, full_range)

    # Walk the back-pointers from the last stop to the origin.
    purchases: list[Purchase] = []
    station, state_index = best_final
    next_position, left_full = total_distance, False
    while True:
        fuel, _, back = states[station][state_index]
        target = capacity if left_full else (next_position - positions[station]) / mpg
        purchases.append(Purchase(index=station, gallons=target - fuel, fuel_on_arrival=fuel))
        if back is None:
            break
        next_position = positions[station]
        station, state_index, left_full = back
    purchases.reverse()
    return purchases


def _cheapest_fill(
    fuels: list[float],
    prefix: list[tuple[float, int]],
    price: float,
    level: float,
    stop_penalty: float,
) -> tuple[float, int] | None:
    """Cheapest way to leave a station holding ``level`` gallons, as ``(cost, state)``.

    Only states that arrive strictly below ``level`` qualify: a stop that buys
    nothing is not a stop. Returns ``None`` when no state qualifies.
    """
    k = bisect_left(fuels, level - _EPSILON) - 1
    if k < 0:
        return None
    return prefix[k][0] + price * level + stop_penalty, prefix[k][1]


def _diagnose_gap(
    positions: Sequence[float], total_distance: float, start_range: float, full_range: float
) -> InfeasibleRouteError:
    """Locate the first stretch that cannot be bridged, for a useful error message."""
    previous, reach = 0.0, start_range
    for position in [*positions, total_distance]:
        if position - previous > reach + _EPSILON:
            return InfeasibleRouteError(previous, position, reach)
        previous, reach = position, full_range
    # Unreachable in practice: the DP only fails when such a gap exists.
    return InfeasibleRouteError(0.0, total_distance, start_range)
