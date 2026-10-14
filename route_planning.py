"""Continuous round trips; API limits must never create extra trips home."""
import math
from concurrent.futures import ThreadPoolExecutor


class RoutePlanningError(ValueError):
    pass


def task_stop_locations(tasks, addresses):
    """Align live OnFleet pins with unique address keys; geocode legacy misses."""
    pins = {}
    for task in tasks:
        try:
            lng, lat = float(task.get('lon')), float(task.get('lat'))
            if (math.isfinite(lng) and math.isfinite(lat)
                    and -180 <= lng <= 180 and -90 <= lat <= 90
                    and (lng, lat) != (0, 0)):
                pins.setdefault(task.get('full'), (lng, lat))
        except (TypeError, ValueError):
            continue
    return tuple(pins.get(address) for address in addresses)


def ordered_route_task_ids(tasks, addresses):
    """Keep all campaigns at a stop together, and dispatch each task once."""
    by_address = {}
    for task in tasks:
        by_address.setdefault(task.get('full'), []).append(str(task['id']).strip())
    return list(dict.fromkeys(task_id for address in addresses
                             for task_id in by_address.get(address, []) if task_id))


def geographic_order(home, stops):
    """Bounded multi-start nearest-neighbor + 2-opt seed for road refinement.

    This is a heuristic, not a claim of a globally optimal road route.
    All stops participate, independent of the API request size.
    """
    points = [home, *stops]
    n = len(stops)
    if n < 2:
        return list(range(n))
    def distance(a, b):
        lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
        h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        return 2 * math.asin(math.sqrt(min(1, h)))
    costs = [[distance(a, b) for b in points] for a in points]
    def length(path):
        return sum(costs[a][b] for a, b in zip(path, path[1:]))
    def improve(order):
        path = [0, *order, 0]
        for _ in range(40):
            best, swap = -1e-12, None
            for i in range(1, n):
                for j in range(i+1, n+1):
                    a, b, c, d = path[i-1], path[i], path[j], path[j+1]
                    delta = costs[a][c] + costs[b][d] - costs[a][b] - costs[c][d]
                    if delta < best:
                        best, swap = delta, (i, j)
            if swap is None:
                break
            i, j = swap
            path[i:j+1] = reversed(path[i:j+1])
        return path
    candidates = [improve(list(range(1, n+1)))]
    # Include nearby and geographically spread starts, bounded to eight.
    starts = sorted(range(1, n+1), key=lambda i: (costs[0][i], i))
    starts = list(dict.fromkeys([starts[0], *[starts[k*(n-1)//7] for k in range(8)]]))
    for first in starts:
        remaining = set(range(1, n+1)) - {first}
        order = [first]
        while remaining:
            nxt = min(remaining, key=lambda i: (costs[order[-1]][i], i))
            remaining.remove(nxt)
            order.append(nxt)
        candidates.append(improve(order))
    return [i-1 for i in min(candidates, key=lambda p: (length(p), p))[1:-1]]


def _road_costs(points, token, request_get):
    """Build a directed road-time matrix across the entire route in 12-stop blocks."""
    n = len(points)
    blocks = [list(range(i, min(i + 12, n))) for i in range(0, n, 12)]
    costs = [[float('inf')] * n for _ in range(n)]
    for i in range(n):
        costs[i][i] = 0.0

    def fetch(pair):
        a, b = pair
        src, dst = blocks[a], blocks[b]
        indices = src if a == b else src + dst
        coords = ';'.join(f'{points[i][0]},{points[i][1]}' for i in indices)
        sources = ';'.join(str(i) for i in range(len(src)))
        targets = ';'.join(str(i if a == b else len(src) + i) for i in range(len(dst)))
        url = (f'https://api.mapbox.com/directions-matrix/v1/mapbox/driving-traffic/{coords}'
               f'?annotations=duration&sources={sources}&destinations={targets}&access_token={token}')
        result = request_get(url, timeout=20).json()
        rows = result.get('durations')
        if (result.get('code') != 'Ok' or not isinstance(rows, list)
                or len(rows) != len(src)
                or any(not isinstance(row, list) or len(row) != len(dst) for row in rows)):
            raise RoutePlanningError('Incomplete road-time matrix; route not dispatched')
        return src, dst, rows

    pairs = [(i, j) for i in range(len(blocks)) for j in range(len(blocks))]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for src, dst, rows in pool.map(fetch, pairs):
            for i, row in zip(src, rows):
                for j, value in zip(dst, row):
                    if i == j:
                        continue
                    if value is None or not math.isfinite(value) or value < 0:
                        raise RoutePlanningError('Unreachable stop in road-time matrix')
                    costs[i][j] = value
    return costs


def _global_road_order(costs, seeds):
    """Directed full-route 2-opt; depot stays fixed at the beginning and end."""
    n = len(costs) - 1

    def score(order):
        path = [0, *[i + 1 for i in order], 0]
        return sum(costs[a][b] for a, b in zip(path, path[1:]))

    def improve(order):
        order = list(order)
        current = score(order)
        for _ in range(30):
            best, candidate = current, None
            for i in range(n - 1):
                for j in range(i + 1, n):
                    trial = order[:i] + order[i:j+1][::-1] + order[j+1:]
                    value = score(trial)
                    if value < best - 0.01:
                        best, candidate = value, trial
            if candidate is None:
                break
            order, current = candidate, best
        return current, order

    candidates = [improve(seed) for seed in seeds]
    for first in sorted(range(n), key=lambda i: costs[0][i+1])[:min(n, 6)]:
        remaining = set(range(n)) - {first}
        order = [first]
        while remaining:
            nxt = min(remaining, key=lambda i: (costs[order[-1]+1][i+1], i))
            order.append(nxt)
            remaining.remove(nxt)
        candidates.append(improve(order))
    return min(candidates, key=lambda result: result[0])[1]


def plan_round_trip(home, stops, token, request_get):
    """Return road miles, driving hours, and indices of every stop exactly once."""
    if not stops:
        return 0.0, 0.0, []
    seed = geographic_order(home, stops)
    # A single global road-time optimization for normal-sized routes.
    # Never dispatch a worse order than the existing stop sequence.
    if len(stops) <= 75:
        costs = _road_costs([home, *stops], token, request_get)
        order = _global_road_order(
            costs, [list(range(len(stops))), seed, list(reversed(seed))])
    else:
        order = None
    def fetch(endpoint, coords, params):
        coord_text = ';'.join(f'{lng},{lat}' for lng, lat in coords)
        response = request_get(
            f'https://api.mapbox.com/{endpoint}/mapbox/driving-traffic/{coord_text}'
            f'?{params}&access_token={token}', timeout=15).json()
        if response.get('code') != 'Ok':
            raise RoutePlanningError('Road routing failed: ' + str(response.get('code')))
        return response
    def refine(offset):
        chunk = seed[offset:offset+10]
        start = home if offset == 0 else stops[seed[offset-1]]
        end = home if offset+10 >= len(seed) else stops[seed[offset+10]]
        result = fetch('optimized-trips/v1', [start, *[stops[i] for i in chunk], end],
                       'source=first&destination=last&roundtrip=false')
        wps = result.get('waypoints', [])
        ranks = [wp.get('waypoint_index') for wp in wps]
        if (not result.get('trips') or len(wps) != len(chunk)+2
                or any(type(rank) is not int for rank in ranks)
                or sorted(ranks) != list(range(len(wps)))
                or ranks[0] != 0 or ranks[-1] != len(wps)-1):
            raise RoutePlanningError('Incomplete optimized stop order')
        return [chunk[i] for i in sorted(range(len(chunk)), key=lambda i: ranks[i+1])]
    if order is None:
        # Large routes use shared chunk endpoints, never intermediate
        # depot returns. The final measured path is still one round trip.
        offsets = list(range(0, len(seed), 10))
        with ThreadPoolExecutor(max_workers=min(4, len(offsets))) as pool:
            order = [i for chunk in pool.map(refine, offsets) for i in chunk]
    if sorted(order) != list(range(len(stops))):
        raise RoutePlanningError('Route did not include every stop once')
    # Directions measures precisely the final dispatch order. Adjacent requests
    # share one endpoint, so each road leg is measured once; home only at ends.
    points = [home, *[stops[i] for i in order], home]
    chunks = [points[i:i+25] for i in range(0, len(points)-1, 24)]
    def measure(coords):
        result = fetch('directions/v5', coords, 'overview=false&steps=false&continue_straight=false')
        routes = result.get('routes') or []
        if not routes:
            raise RoutePlanningError('No road route')
        miles = routes[0]['distance'] * 0.000621371
        hours = routes[0]['duration'] / 3600
        if not (math.isfinite(miles) and math.isfinite(hours) and miles >= 0 and hours >= 0):
            raise RoutePlanningError('Invalid road totals')
        return miles, hours
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        totals = list(pool.map(measure, chunks))
    return sum(x[0] for x in totals), sum(x[1] for x in totals), order
