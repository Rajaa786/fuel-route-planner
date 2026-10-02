import numpy as np
import pytest
from scipy.spatial import cKDTree

from domain import geometry
from tests.helpers import encode_polyline


def test_decode_polyline_known_vector():
    # Canonical example from Google's polyline documentation (precision 5).
    decoded = geometry.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    np.testing.assert_allclose(decoded, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])


def test_decode_polyline_round_trips_random_paths():
    rng = np.random.default_rng(1)
    points = np.column_stack((rng.uniform(25, 49, 2000), rng.uniform(-124, -67, 2000)))
    decoded = geometry.decode_polyline(encode_polyline(points))
    np.testing.assert_allclose(decoded, points, atol=1e-6)


def test_decode_polyline_edge_cases():
    assert geometry.decode_polyline("").shape == (0, 2)
    with pytest.raises(ValueError):
        geometry.decode_polyline("_p~iF~ps|U_")  # truncated run


def test_cumulative_miles_matches_known_distance():
    # New York City -> Los Angeles great-circle distance is ~2,446 miles.
    path = np.array([[40.7128, -74.0060], [34.0522, -118.2437]])
    assert geometry.cumulative_miles(path)[-1] == pytest.approx(2446, abs=5)
    assert geometry.cumulative_miles(path[:1]).tolist() == [0.0]


def test_resample_bounds_spacing_and_keeps_endpoints():
    path = np.array([[40.0, -100.0], [40.0, -99.0], [41.0, -99.0]])
    cumulative = geometry.cumulative_miles(path)
    sampled, marks = geometry.resample(path, cumulative, spacing=0.25)
    np.testing.assert_allclose(sampled[0], path[0])
    np.testing.assert_allclose(sampled[-1], path[-1])
    assert marks[0] == 0.0 and marks[-1] == pytest.approx(cumulative[-1])
    assert np.diff(marks).max() <= 0.25 + 1e-9
    assert np.diff(geometry.cumulative_miles(sampled)).max() <= 0.26


def test_unit_sphere_chord_distance_agrees_with_haversine():
    a, b = np.array([[36.1699, -115.1398]]), np.array([[39.7392, -104.9903]])
    chord = np.linalg.norm(
        geometry.to_unit_xyz(a[:, 0], a[:, 1]) - geometry.to_unit_xyz(b[:, 0], b[:, 1]), axis=1
    )
    haversine = geometry.cumulative_miles(np.vstack((a, b)))[-1]
    assert geometry.chord_to_miles(chord)[0] == pytest.approx(haversine, rel=1e-9)
    assert geometry.miles_to_chord(haversine) == pytest.approx(chord[0], rel=1e-9)


def test_kdtree_on_unit_sphere_finds_points_within_radius():
    route = geometry.to_unit_xyz(np.array([40.0, 40.0]), np.array([-100.0, -99.9]))
    near = geometry.to_unit_xyz(np.array([40.02]), np.array([-100.0]))  # ~1.4 mi north
    far = geometry.to_unit_xyz(np.array([40.5]), np.array([-100.0]))  # ~35 mi north
    tree = cKDTree(route)
    limit = geometry.miles_to_chord(5.0)
    assert np.isfinite(tree.query(near, distance_upper_bound=limit)[0][0])
    assert np.isinf(tree.query(far, distance_upper_bound=limit)[0][0])


def test_simplify_drops_collinear_points_and_keeps_corners():
    straight = np.column_stack((np.full(50, 40.0), np.linspace(-100, -99, 50)))
    corner = np.column_stack((np.linspace(40.0, 41.0, 50), np.full(50, -99.0)))
    path = np.vstack((straight, corner[1:]))
    simplified = geometry.simplify(path, tolerance_miles=0.05)
    assert len(simplified) < 10
    np.testing.assert_allclose(simplified[0], path[0])
    np.testing.assert_allclose(simplified[-1], path[-1])
    assert any(np.allclose(point, [40.0, -99.0]) for point in simplified)
