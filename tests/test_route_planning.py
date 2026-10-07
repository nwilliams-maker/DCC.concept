import math
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from route_planning import geographic_order, plan_round_trip, RoutePlanningError, task_stop_locations


def path_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


class FakeRoads:
    def __init__(self, reverse=False, fail=None):
        self.calls = []
        self.reverse = reverse
        self.fail = fail

    def __call__(self, url, timeout):
        path = urlsplit(url).path
        coords = [tuple(map(float, c.split(','))) for c in path.split('/')[-1].split(';')]
        optimization = 'optimized-trips' in path
        self.calls.append((optimization, coords))
        if self.fail == ('optimization' if optimization else 'directions'):
            result = {'code': 'NoRoute'}
        elif optimization:
            assert len(coords) <= 12
            middle = list(range(1, len(coords)-1))
            if self.reverse:
                middle.reverse()
            ranks = [0, *middle, len(coords)-1]
            result = {'code': 'Ok', 'trips': [{}],
                      'waypoints': [{'waypoint_index': rank} for rank in ranks]}
        else:
            assert 2 <= len(coords) <= 25
            distance = path_length(coords)*1000
            result = {'code': 'Ok', 'routes': [{'distance': distance, 'duration': distance/10}]}
        return SimpleNamespace(json=lambda: result)


@pytest.mark.parametrize('count', [1, 10, 11, 24, 25, 26, 52, 96, 100])
def test_every_stop_once_continuous_trip_and_measured_final_order(count):
    home = (-111., 32.)
    stops = [(-111.+i*.001, 32.1+(i%7)*.001) for i in range(count)]
    roads = FakeRoads(reverse=True)
    miles, hours, order = plan_round_trip(home, stops, 'test', roads)
    assert sorted(order) == list(range(count))
    measured = [coords for optimization, coords in roads.calls if not optimization]
    # Calls can finish in any order; sort by their first endpoint in final path.
    expected = [home, *[stops[i] for i in order], home]
    measured.sort(key=lambda coords: expected.index(coords[0]))
    combined = measured[0] + [point for chunk in measured[1:] for point in chunk[1:]]
    assert combined == expected
    assert sum(chunk.count(home) for chunk in measured) == 2
    assert miles == pytest.approx(path_length(expected)*1000*.000621371)
    assert hours == pytest.approx(path_length(expected)*1000/10/3600)
    optimization_calls = [coords for optimization, coords in roads.calls if optimization]
    assert sum(chunk.count(home) for chunk in optimization_calls) == 2


def test_global_seed_fixes_scattered_tucson_order():
    home = (-111., 32.25)
    # Stops alternate across town, like concatenated unrelated bundle pieces.
    west = [(-111.15+i*.001, 32.3+i*.001) for i in range(20)]
    east = [(-110.8+i*.001, 32.15+i*.001) for i in range(20)]
    stops = [point for pair in zip(west, east) for point in pair]
    order = geographic_order(home, stops)
    assert sorted(order) == list(range(40))
    assert path_length([home, *[stops[i] for i in order], home]) < path_length([home, *stops, home])*.2


@pytest.mark.parametrize('failure', ['optimization', 'directions'])
def test_road_failure_never_returns_partial_totals_or_guessed_order(failure):
    with pytest.raises(RoutePlanningError):
        plan_round_trip((0, 0), [(i+1., 1.) for i in range(30)], 'test', FakeRoads(fail=failure))


def test_malformed_optimizer_order_rejected():
    def get(url, timeout):
        return SimpleNamespace(json=lambda: {'code': 'Ok', 'trips': [{}],
            'waypoints': [{'waypoint_index': 0}]*4})
    with pytest.raises(RoutePlanningError):
        plan_round_trip((0, 0), [(1, 1), (2, 2)], 'test', get)


def test_large_geographic_seed_is_bounded_and_not_worse_than_input():
    home = (-111., 32.)
    stops = [(-111.+math.sin(i)*.2, 32.+math.cos(i)*.2) for i in range(100)]
    started = time.monotonic()
    order = geographic_order(home, stops)
    assert time.monotonic()-started < 3
    assert path_length([home, *[stops[i] for i in order], home]) < path_length([home, *stops, home])


def test_failed_measurement_is_not_cached_and_service_pay_includes_all_stops():
    import ast
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    source = Path('tactical_workspace_master_rw.py').read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'get_gmaps')
    cache = {}
    geocodes = {'home': (-111., 32.), **{f'stop-{i}': (-111.+i*.001, 32.1) for i in range(52)}}
    roads = FakeRoads(fail='directions')
    scope = {'time': time, 'ThreadPoolExecutor': ThreadPoolExecutor, 'MAPBOX_TOKEN': 'test',
             '_gmaps_route_cache': lambda: cache, '_mapbox_geocode_cache': lambda: geocodes,
             '_mapbox_geocode': lambda address, **kwargs: geocodes[address],
             'requests': SimpleNamespace(get=roads), '_log_err': lambda *args: None,
             'plan_round_trip': plan_round_trip, 'task_stop_locations': task_stop_locations}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'routing', 'exec'), scope)
    get = scope['get_gmaps']
    stops = tuple(f'stop-{i}' for i in range(52))
    assert get('home', stops) == (0, 0, '0h 0m', [])
    assert not cache
    roads.fail = None
    miles, total_hours, text, order = get('home', stops)
    expected = [geocodes['home'], *[geocodes[stops[i]] for i in order], geocodes['home']]
    driving_hours = path_length(expected)*1000/10/3600
    assert total_hours == pytest.approx(driving_hours + 52/6)
    assert total_hours * 25 == pytest.approx((driving_hours + 52/6)*25)
    assert len(order) == 52
    calls = len(roads.calls)
    assert get('home', stops) == (miles, total_hours, text, order)
    assert len(roads.calls) == calls


def test_live_task_pins_are_aligned_without_changing_address_identity():
    tasks = [{'full': 'b', 'lat': 32.2, 'lon': -111.2},
             {'full': 'a', 'lat': 32.1, 'lon': -111.1},
             {'full': 'a', 'lat': 32.1, 'lon': -111.1},
             {'full': 'legacy', 'lat': 0, 'lon': 0},
             {'full': 'bad', 'lat': float('nan'), 'lon': -111}]
    assert task_stop_locations(tasks, ['a', 'b', 'legacy', 'bad', 'missing']) == (
        (-111.1, 32.1), (-111.2, 32.2), None, None, None)


def test_live_pin_changes_invalidate_routing_cache_and_override_old_geocoding():
    import ast
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    node = next(n for n in ast.parse(Path('tactical_workspace_master_rw.py').read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'get_gmaps')
    cache = {}
    geocodes = {'home': (-111., 32.), 'stop': (-110., 33.)}
    roads = FakeRoads()
    scope = {'time': time, 'ThreadPoolExecutor': ThreadPoolExecutor, 'MAPBOX_TOKEN': 'test',
             '_gmaps_route_cache': lambda: cache, '_mapbox_geocode_cache': lambda: geocodes,
             '_mapbox_geocode': lambda address, **kwargs: geocodes[address],
             'requests': SimpleNamespace(get=roads), '_log_err': lambda *args: None,
             'plan_round_trip': plan_round_trip, 'task_stop_locations': task_stop_locations}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'routing', 'exec'), scope)
    get = scope['get_gmaps']
    tasks = [{'full': 'stop', 'lat': 32.01, 'lon': -111.01}]
    first = get('home', ('stop',), tasks=tasks)
    assert roads.calls[0][1][1] == (-111.01, 32.01)
    tasks[0]['lon'] = -111.02
    second = get('home', ('stop',), tasks=tasks)
    assert second[1] > first[1]
    assert len(cache) == 2


def test_dispatch_order_groups_campaigns_without_duplicating_task_ids():
    from route_planning import ordered_route_task_ids
    tasks = [{'id': 'a1', 'full': 'a'}, {'id': 'b1', 'full': 'b'},
             {'id': 'a2', 'full': 'a'}, {'id': 'b1', 'full': 'b'},
             {'id': 'c1', 'full': 'c'}]
    assert ordered_route_task_ids(tasks, ['c', 'b', 'a']) == ['c1', 'b1', 'a1', 'a2']
