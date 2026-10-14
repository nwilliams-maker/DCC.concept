"""Regression tests for Mapbox limits and one continuous measured road trip.

Never call live Mapbox: this fake rejects request sizes/profiles exactly as the
documented Matrix (25 driving/10 traffic), Optimization v1 (12), and Directions
(25) endpoints do. The returned metrics are synthetic *road API responses*.
"""
import math
import threading
from urllib.parse import urlsplit, parse_qs

import pytest

from route_planning import (
    RoutePlanningError,
    _road_costs,
    geographic_order,
    ordered_route_task_ids,
    plan_round_trip,
    task_stop_locations,
)


HOME = (-87.6298, 41.8781)


def stops_for(count):
    # Unique coordinates distributed around Chicago, none equal to home.
    return [(-87.7 + (i % 15) * 0.005,
             41.75 + (i // 15) * 0.007 + (i % 4) * 0.0001)
            for i in range(count)]


def leg_cost(a, b):
    # Test-only deterministic synthetic *road* service response.
    return 300.0 + (abs(a[0] - b[0]) + abs(a[1] - b[1])) * 85000.0


class FakeResult:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class StrictMapbox:
    def __init__(self, *, fail_matrix=False, fail_optimization=False,
                 fail_traffic_directions=False, fail_all_directions=False):
        self.fail_matrix = fail_matrix
        self.fail_optimization = fail_optimization
        self.fail_traffic_directions = fail_traffic_directions
        self.fail_all_directions = fail_all_directions
        self.calls = []
        self.direction_paths = []
        self.lock = threading.Lock()

    def __call__(self, url, timeout):
        urlparts = urlsplit(url)
        params = parse_qs(urlparts.query)
        assert 'access_token' in params
        assert timeout <= 20
        coords = [tuple(map(float, pair.split(',')))
                  for pair in urlparts.path.rsplit('/', 1)[-1].split(';')]
        path = urlparts.path
        with self.lock:
            self.calls.append((path, len(coords)))
        if '/directions-matrix/v1/' in path:
            # This fails against the pre-fix implementation: it sent as many
            # as 24 coordinates on the traffic matrix, whose cap is TEN.
            profile = 'driving-traffic' if '/driving-traffic/' in path else 'driving'
            limit = 10 if profile == 'driving-traffic' else 25
            if len(coords) > limit:
                return FakeResult({'code': 'InvalidInput', 'message': 'Too many coordinates'})
            if self.fail_matrix:
                return FakeResult({'code': 'InvalidInput', 'message': 'Matrix unavailable'})
            src = [int(x) for x in params['sources'][0].split(';')]
            dst = [int(x) for x in params['destinations'][0].split(';')]
            rows = [[0.0 if i == j else leg_cost(coords[i], coords[j])
                     for j in dst] for i in src]
            return FakeResult({'code': 'Ok', 'durations': rows})
        if '/optimized-trips/v1/' in path:
            if len(coords) > 12:
                return FakeResult({'code': 'InvalidInput', 'message': 'Optimization max 12'})
            if '/driving-traffic/' in path:
                return FakeResult({'code': 'ProfileNotFound', 'message': 'Traffic optimization unavailable'})
            if self.fail_optimization:
                return FakeResult({'code': 'NoTrips', 'message': 'Optimization temporarily unavailable'})
            return FakeResult({
                'code': 'Ok',
                'waypoints': [{'waypoint_index': i} for i in range(len(coords))],
                'trips': [{'distance': 1.0, 'duration': 1.0}],
            })
        if '/directions/v5/' in path:
            if len(coords) > 25:
                return FakeResult({'code': 'InvalidInput', 'message': 'Directions max 25'})
            if self.fail_all_directions:
                return FakeResult({'code': 'NoRoute', 'message': 'Disconnected roads'})
            if self.fail_traffic_directions and '/driving-traffic/' in path:
                return FakeResult({'code': 'NoRoute', 'message': 'Traffic roads unavailable'})
            with self.lock:
                self.direction_paths.append(coords)
            distance = sum(leg_cost(a, b) for a, b in zip(coords, coords[1:]))
            return FakeResult({'code': 'Ok', 'routes': [{
                'distance': distance, 'duration': distance / 16.0,
            }]})
        raise AssertionError('Unexpected Mapbox endpoint: ' + path)


def assert_round_trip(n, *, service=None):
    service = service or StrictMapbox()
    stops = stops_for(n)
    miles, hours, order = plan_round_trip(HOME, stops, 'test-token', service)
    assert sorted(order) == list(range(n))
    assert len(order) == n
    assert miles > 0.0
    assert hours > 0.0
    assert service.direction_paths

    # Stitch Directions calls to reproduce the exact measured route.
    measured = list(service.direction_paths[0])
    for segment in service.direction_paths[1:]:
        # Concurrent calls may finish in different order, so sorting below
        # matches them by the shared endpoint instead of completion order.
        pass
    segments = [list(segment) for segment in service.direction_paths]
    reconstructed = [HOME]
    while segments:
        next_idx = next((i for i, seg in enumerate(segments)
                         if seg[0] == reconstructed[-1]), None)
        assert next_idx is not None, 'Chunks do not form a continuous trip'
        seg = segments.pop(next_idx)
        reconstructed.extend(seg[1:])
    assert reconstructed == [HOME, *[stops[i] for i in order], HOME]
    assert reconstructed.count(HOME) == 2, 'No intermediate return to home'

    expected_distance = sum(leg_cost(a, b)
                            for a, b in zip(reconstructed, reconstructed[1:]))
    assert math.isclose(miles, expected_distance * 0.000621371, rel_tol=1e-12)
    assert math.isclose(hours, expected_distance / 16.0 / 3600.0, rel_tol=1e-12)
    return service


@pytest.mark.parametrize('n', [1, 2, 9, 12, 13, 23, 35])
def test_small_and_medium_routes_use_valid_road_matrix(n):
    service = assert_round_trip(n)
    matrices = [(path, count) for path, count in service.calls
                if '/directions-matrix/v1/' in path]
    assert matrices
    assert all('/mapbox/driving/' in path and count <= 25
               for path, count in matrices)


@pytest.mark.parametrize('n', [36, 59, 76, 110])
def test_large_routes_avoid_matrix_explosion_and_return_home_once(n):
    service = assert_round_trip(n)
    assert not any('/directions-matrix/v1/' in path for path, _ in service.calls)
    optimizations = [(path, count) for path, count in service.calls
                     if '/optimized-trips/v1/' in path]
    assert optimizations
    assert all('/mapbox/driving/' in path and count <= 12
               for path, count in optimizations)


def test_matrix_failure_uses_road_optimization_and_measures_all_stops():
    service = assert_round_trip(20, service=StrictMapbox(fail_matrix=True))
    assert any('/optimized-trips/v1/mapbox/driving/' in path
               for path, _ in service.calls)


def test_optimization_failure_falls_back_to_geographic_order_but_road_mileage():
    assert_round_trip(59, service=StrictMapbox(fail_optimization=True))


def test_traffic_directions_failure_retries_on_real_driving_roads():
    service = assert_round_trip(59, service=StrictMapbox(fail_traffic_directions=True))
    assert any('/directions/v5/mapbox/driving-traffic/' in path
               for path, _ in service.calls)
    assert any('/directions/v5/mapbox/driving/' in path
               for path, _ in service.calls)


def test_both_road_profiles_unreachable_never_invent_miles():
    service = StrictMapbox(fail_all_directions=True)
    with pytest.raises(RoutePlanningError, match='Road directions failed'):
        plan_round_trip(HOME, stops_for(13), 'test-token', service)


def test_empty_route_keeps_zero_without_any_mapbox_call():
    service = StrictMapbox()
    assert plan_round_trip(HOME, [], 'test-token', service) == (0.0, 0.0, [])
    assert not service.calls


def test_task_location_alignment_and_task_deduplication():
    tasks = [
        {'id': 'a', 'full': 'Address A', 'lon': -87.60, 'lat': 41.80},
        {'id': 'b', 'full': 'Address A', 'lon': -87.61, 'lat': 41.81},
        {'id': 'c', 'full': 'Address B', 'lon': -87.62, 'lat': 41.82},
        {'id': 'a', 'full': 'Address A', 'lon': -87.60, 'lat': 41.80},
    ]
    addresses = ['Address B', 'Address A']
    assert task_stop_locations(tasks, addresses) == (
        (-87.62, 41.82), (-87.60, 41.80))
    assert ordered_route_task_ids(tasks, addresses) == ['c', 'a', 'b']


def test_diagnostics_report_real_exception_instead_of_generic_type():
    from pathlib import Path
    code = Path('tactical_workspace_master_rw.py').read_text(encoding='utf8')
    assert '_log_err("get_gmaps", e)' in code
    assert 'continuous-v3' in code
