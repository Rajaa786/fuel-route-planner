"""Vectorised geometry helpers for route polylines.

Pure numpy; no Django imports, so everything here is trivially unit-testable.
Coordinates are ``(latitude, longitude)`` in degrees unless stated otherwise.
"""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google/OSRM encoded polyline into an ``(N, 2)`` lat/lon array.

    Vectorised: a coast-to-coast route (~35k points) decodes in about a
    millisecond, versus ~25 ms for the textbook character loop.
    """
    if not encoded:
        return np.empty((0, 2), dtype=np.float64)

    chunks = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    if chunks.min() < 0:
        raise ValueError("Invalid character in encoded polyline.")

    # Each number is a run of 5-bit chunks; bit 0x20 flags "more chunks follow".
    is_last = chunks < 0x20
    if not is_last[-1]:
        raise ValueError("Truncated encoded polyline.")
    starts = np.concatenate(([0], np.flatnonzero(is_last)[:-1] + 1))
    index_in_run = np.arange(chunks.size) - np.repeat(
        starts, np.diff(np.append(starts, chunks.size))
    )
    values = np.add.reduceat((chunks & 0x1F) << (5 * index_in_run), starts)

    # Zig-zag decode, then undo the delta encoding.
    deltas = np.where(values & 1, ~(values >> 1), values >> 1)
    if deltas.size % 2:
        raise ValueError("Encoded polyline has an odd number of values.")
    return np.cumsum(deltas.reshape(-1, 2), axis=0) / float(10**precision)


def cumulative_miles(points: np.ndarray) -> np.ndarray:
    """Cumulative great-circle distance (miles) along a lat/lon path; starts at 0."""
    if len(points) < 2:
        return np.zeros(len(points), dtype=np.float64)
    lat = np.radians(points[:, 0])
    lon = np.radians(points[:, 1])
    a = (
        np.sin(np.diff(lat) / 2) ** 2
        + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    )
    segments = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))
    return np.concatenate(([0.0], np.cumsum(segments)))


def resample(
    points: np.ndarray, cumulative: np.ndarray, spacing: float
) -> tuple[np.ndarray, np.ndarray]:
    """Resample a path at a uniform ``spacing`` (same unit as ``cumulative``).

    Routing engines emit long straight segments as two far-apart vertices, so
    "nearest vertex" can be miles from the true nearest point on the road.
    Uniform samples bound that error to ``spacing / 2``.
    Returns ``(sampled_points, sampled_cumulative)``; both endpoints are kept.
    """
    total = float(cumulative[-1])
    count = max(int(np.ceil(total / spacing)), 1) + 1
    marks = np.linspace(0.0, total, count)
    sampled = np.column_stack(
        (np.interp(marks, cumulative, points[:, 0]), np.interp(marks, cumulative, points[:, 1]))
    )
    return sampled, marks


def to_unit_xyz(latitude: np.ndarray, longitude: np.ndarray) -> np.ndarray:
    """Project lat/lon degrees onto the unit sphere.

    Euclidean (chord) distance between unit vectors is monotonic in great-circle
    distance, which lets a plain KD-tree answer spherical nearest-neighbour
    queries exactly, with no projection distortion and no antimeridian issues.
    """
    lat = np.radians(np.asarray(latitude, dtype=np.float64))
    lon = np.radians(np.asarray(longitude, dtype=np.float64))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def miles_to_chord(miles: float) -> float:
    """Unit-sphere chord length subtending a great-circle distance in miles."""
    return float(2 * np.sin(miles / EARTH_RADIUS_MILES / 2))


def chord_to_miles(chord: np.ndarray) -> np.ndarray:
    """Inverse of :func:`miles_to_chord`, vectorised."""
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2, 0.0, 1.0))


def simplify(points: np.ndarray, tolerance_miles: float) -> np.ndarray:
    """Douglas-Peucker simplification; returns the retained rows of ``points``.

    Used only to shrink the geometry sent to clients. The planner itself always
    works on the full-resolution route.
    """
    count = len(points)
    if count < 3:
        return points

    # Local equirectangular projection (miles): accurate enough for a tolerance test.
    lat = np.radians(points[:, 0])
    x = np.radians(points[:, 1]) * np.cos(lat.mean()) * EARTH_RADIUS_MILES
    y = lat * EARTH_RADIUS_MILES

    keep = np.zeros(count, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        dx, dy = x[last] - x[first], y[last] - y[first]
        px, py = x[first + 1 : last] - x[first], y[first + 1 : last] - y[first]
        length = np.hypot(dx, dy)
        distances = np.hypot(px, py) if length == 0 else np.abs(dx * py - dy * px) / length
        farthest = int(np.argmax(distances))
        if distances[farthest] > tolerance_miles:
            split = first + 1 + farthest
            keep[split] = True
            stack.append((first, split))
            stack.append((split, last))
    return points[keep]
