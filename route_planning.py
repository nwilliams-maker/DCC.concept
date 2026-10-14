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
    """Use 12-point blocks and the 25-coordinate *driving* Matrix API.

    Traffic-aware matrices are limited to TEN coordinates, including when
    selecting sources and destinations. The cross-block requests below may
    contain 24 coordinates; using driving-traffic made those requests invalid.
    Directions still uses traffic-aware road miles and travel times.
    """
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
        url = (f'https://api.mapbox.com/directions-matrix/v1/mapbox/driving/{coords}'
               f'?annotations=duration&sources={sources}&destinations={targets}&access_token={token}')
        try:
            result = request_get(url, timeout=20).json()
        except Exception as exc:
            raise RoutePlanningError(f'Matrix request failed: {type(exc).__name__}') from exc
        if not isinstance(result, dict):
            raise RoutePlanningError('Matrix returned invalid JSON')
        rows = result.get('durations')
        if (result.get('code') != 'Ok' or not isinstance(rows, list)
                or len(rows) != len(src)
                or any(not isinstance(row, list) or len(row) != len(dst) for row in rows)):
            message = str(result.get('message') or '')[:140]
            raise RoutePlanningError(
                f'Matrix unavailable ({result.get("code", "unknown")}: {message})')
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
    """Return road miles, driving hours, and an order visiting every stop once.

    The trip ALWAYS begins and ends at home. The global Mapbox Matrix is used
    for modest routes; larger routes use geographic sequencing with local road
    optimization to avoid matrix rate limits. All reported miles and hours are
    measured on Mapbox Directions road routes, never straight-line estimates.
    """
    if not stops:
        return 0.0, 0.0, []
    seed = geographic_order(home, stops)

    # Matrix is optional route-order refinement, NOT a dependency for mileage.
    # With 12-point blocks, 35 stops require <=9 driving-matrix calls; 75
    # stops would require 49, competing with the provider's 60/minute limit.
    order = None
    if len(stops) <= 35:
        try:
            costs = _road_costs([home, *stops], token, request_get)
            order = _global_road_order(
                costs, [list(range(len(stops))), seed, list(reversed(seed))])
        except Exception as exc:
            print(f'[route_planning] Matrix fallback: {type(exc).__name__}: {exc}', flush=True)

    def fetch(endpoint, profile, coords, params):
        coord_text = ';'.join(f'{lng},{lat}' for lng, lat in coords)
        url = (f'https://api.mapbox.com/{endpoint}/mapbox/{profile}/{coord_text}'
               f'?{params}&access_token={token}')
        try:
            response = request_get(url, timeout=20).json()
        except Exception as exc:
            raise RoutePlanningError(
                f'{endpoint}/{profile} request failed: {type(exc).__name__}') from exc
        if not isinstance(response, dict):
            raise RoutePlanningError(f'{endpoint}/{profile} returned invalid JSON')
        if response.get('code') != 'Ok':
            code = str(response.get('code') or 'unknown')
            message = str(response.get('message') or '')[:140]
            raise RoutePlanningError(f'{endpoint}/{profile}: {code}: {message}')
        return response

    def refine(offset):
        # Optimization v1 accepts up to 12 coordinates per request. Only the
        # first/last are fixed endpoints; no chunk returns to the depot.
        chunk = seed[offset:offset+10]
        start = home if offset == 0 else stops[seed[offset-1]]
        end = home if offset+10 >= len(seed) else stops[seed[offset+10]]
        try:
            # Optimization v1 is a *driving* route-order service. Traffic
            # profile support is not reliable; use traffic-aware Directions
            # below for actual road mileage and duration.
            result = fetch('optimized-trips/v1', 'driving',
                           [start, *[stops[i] for i in chunk], end],
                           'source=first&destination=last&roundtrip=false')
            wps = result.get('waypoints', [])
            ranks = [wp.get('waypoint_index') for wp in wps]
            if (not result.get('trips') or len(wps) != len(chunk)+2
                    or any(type(rank) is not int for rank in ranks)
                    or sorted(ranks) != list(range(len(wps)))
                    or ranks[0] != 0 or ranks[-1] != len(wps)-1):
                raise RoutePlanningError('Incomplete optimized stop order')
            return [chunk[i] for i in sorted(range(len(chunk)),
                                             key=lambda i: ranks[i+1])]
        except Exception as exc:
            # A failed optimization must not invalidate the full route. Keep
            # the existing global geographic seed for this chunk, and still
            # measure every stop on actual roads below.
            print(f'[route_planning] Optimization chunk {offset // 10 + 1} fallback: '
                  f'{type(exc).__name__}: {exc}', flush=True)
            return chunk

    if order is None:
        offsets = list(range(0, len(seed), 10))
        with ThreadPoolExecutor(max_workers=min(4, len(offsets))) as pool:
            order = [i for chunk in pool.map(refine, offsets) for i in chunk]

    if sorted(order) != list(range(len(stops))):
        raise RoutePlanningError('Route did not include every stop exactly once')

    # Every chunk shares exactly one endpoint with the next, so the complete
    # measured path is home -> ALL stops -> home, without intermediate returns.
    points = [home, *[stops[i] for i in order], home]
    chunks = [points[i:i+25] for i in range(0, len(points)-1, 24)]

    def measure(coords):
        def measure_profile(profile):
            result = fetch('directions/v5', profile, coords,
                           'overview=false&steps=false&continue_straight=false')
            routes = result.get('routes') or []
            if not routes:
                raise RoutePlanningError(f'Directions/{profile}: no road route')
            try:
                miles = float(routes[0]['distance']) * 0.000621371
                hours = float(routes[0]['duration']) / 3600
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                raise RoutePlanningError('Directions missing road totals') from exc
            if not (math.isfinite(miles) and math.isfinite(hours)
                    and miles >= 0 and hours >= 0):
                raise RoutePlanningError('Directions returned invalid road totals')
            return miles, hours

        try:
            return measure_profile('driving-traffic')
        except RoutePlanningError as traffic_error:
            # Traffic routing can fail for specific regions or coordinates.
            # The driving profile is a legitimate road-distance alternative,
            # never a geodesic approximation.
            try:
                return measure_profile('driving')
            except RoutePlanningError as driving_error:
                raise RoutePlanningError(
                    f'Road directions failed ({len(coords)} waypoints): '
                    f'traffic: {traffic_error}; driving: {driving_error}') from driving_error

    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        totals = list(pool.map(measure, chunks))
    return sum(x[0] for x in totals), sum(x[1] for x in totals), order
