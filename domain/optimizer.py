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

Detours
-------
A station may sit ``detour`` miles off the route. Visiting it costs that
distance twice (out and back), so the leg between consecutive stops ``i -> j``
is ``(position_j + detour_j) - (position_i - detour_i)``. The structure lemma
only concerns consecutive stops and the tank, not the shape of the road, so the
same DP applies with those leg lengths. With all detours zero it reduces to the
plain mile-marker problem.
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

# Tolerance in gallons for float comparisons; far below a cent of fuel. Every
# feasibility test is made in gallons so that one tolerance means one thing.
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
    detours: Sequence[float] | None = None,
) -> list[Purchase]:
    """Return the optimal purchases, in route order.

    ``positions`` must be strictly increasing mile markers within
    ``[0, total_distance]``; ``prices`` are dollars per gallon; ``detours`` are
    one-way miles from the route to each station (default: none). Raises
    :class:`InfeasibleRouteError` if the destination cannot be reached.
    """
    count = len(positions)
    if detours is None:
        detours = [0.0] * count
    if len(prices) != count or len(detours) != count:
        raise ValueError("positions, prices and detours must have the same length.")
    if any(positions[i] >= positions[i + 1] for i in range(count - 1)):
        raise ValueError("positions must be strictly increasing.")
    if any(detour < 0 for detour in detours):
        raise ValueError("detours must not be negative.")
    if not 0 <= start_fuel_gallons <= tank_capacity_gallons:
        raise ValueError("start_fuel_gallons must be within the tank capacity.")
    if stop_penalty < 0:
        # A negative penalty would reward pointless stops; the DP assumes it never pays to
        # stop without buying.
        raise ValueError("stop_penalty must not be negative.")

    capacity = tank_capacity_gallons
    mpg = miles_per_gallon

    # Twice the tolerance on purpose: a purchase must exceed one tolerance to count as a
    # stop, so a shortfall within float noise of that threshold must land on this side.
    if total_distance / mpg <= start_fuel_gallons + 2 * _EPSILON:
        return []

    # Where the vehicle is, in route miles, when it reaches a pump and when it is back on
    # the route having left it. A leg from stop i to stop j burns arrive[j] - leave[i] miles.
    arrive = [position + detour for position, detour in zip(positions, detours, strict=True)]
    leave = [position - detour for position, detour in zip(positions, detours, strict=True)]

    # states[i]: (arrival_fuel, cost_so_far, back_pointer) for "we stop at i".
    # cost_so_far excludes the purchase (and penalty) at i itself.
    # back_pointer is None (came from the origin) or (prev_station, prev_state, left_full).
    states: list[list[tuple[float, float, tuple[int, int, bool] | None]]] = [
        [] for _ in range(count)
    ]
    for j in range(count):
        if positions[j] / mpg > start_fuel_gallons + _EPSILON:
            break  # markers only grow from here, and a detour never shortens a leg
        needed = arrive[j] / mpg
        if needed <= start_fuel_gallons + _EPSILON:
            states[j].append((start_fuel_gallons - needed, 0.0, None))

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

        for j in range(i + 1, count):
            if (positions[j] - leave[i]) / mpg > capacity + _EPSILON:
                break  # beyond a full tank even without j's own detour
            needed = (arrive[j] - leave[i]) / mpg
            if needed > capacity + _EPSILON:
                continue
            if prices[j] > price:
                if leave_full is not None:
                    states[j].append((capacity - needed, leave_full[0], (i, leave_full[1], True)))
            else:
                option = _cheapest_fill(fuels, prefix, price, needed, stop_penalty)
                if option is not None and (
                    arrive_empty[j] is None or option[0] < arrive_empty[j][0]
                ):
                    arrive_empty[j] = (option[0], (i, option[1], False))

        final_leg = (total_distance - leave[i]) / mpg
        if final_leg <= capacity + _EPSILON:
            option = _cheapest_fill(fuels, prefix, price, final_leg, stop_penalty)
            if option is not None and option[0] < best_total:
                best_total, best_final = option[0], (i, option[1])

    if best_final is None:
        raise _diagnose_gap(
            positions, arrive, leave, total_distance, start_fuel_gallons, capacity, mpg
        )

    # Walk the back-pointers from the last stop to the origin.
    purchases: list[Purchase] = []
    station, state_index = best_final
    next_arrival, left_full = total_distance, False
    while True:
        fuel, _, back = states[station][state_index]
        target = capacity if left_full else (next_arrival - leave[station]) / mpg
        purchases.append(Purchase(index=station, gallons=target - fuel, fuel_on_arrival=fuel))
        if back is None:
            break
        next_arrival = arrive[station]
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
    positions: Sequence[float],
    arrive: Sequence[float],
    leave: Sequence[float],
    total_distance: float,
    start_fuel: float,
    capacity: float,
    mpg: float,
) -> InfeasibleRouteError:
    """Locate the stretch that cannot be bridged, for a useful error message.

    Finds the furthest station that can be reached at all; the gap runs from
    there to the next point on the route (a station or the destination).
    """
    reachable = [False] * len(positions)
    for j in range(len(positions)):
        reachable[j] = arrive[j] / mpg <= start_fuel + _EPSILON or any(
            reachable[i] and (arrive[j] - leave[i]) / mpg <= capacity + _EPSILON for i in range(j)
        )
    frontier = max((j for j, ok in enumerate(reachable) if ok), default=None)
    gap_start = 0.0 if frontier is None else positions[frontier]
    reach = (start_fuel if frontier is None else capacity) * mpg
    following = (frontier + 1) if frontier is not None else 0
    gap_end = positions[following] if following < len(positions) else total_distance
    return InfeasibleRouteError(gap_start, gap_end, reach)
