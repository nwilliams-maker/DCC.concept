"""Free map provider (Census + OpenRouteService + OSRM). Never calls live APIs."""
import sqlalchemy as sa

from migration import map_provider as mp
from route_planning import geographic_order

HOME = (-96.80, 32.78)
STOPS = [(-96.70, 32.90), (-97.33, 32.75), (-96.90, 32.70), (-96.75, 32.95)]


class R:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def census_body(x, y, state="TX", zip_code="75201"):
    return {"result": {"addressMatches": [{
        "coordinates": {"x": x, "y": y},
        "addressComponents": {"state": state, "zip": zip_code}}]}}


# --- geocoding ------------------------------------------------------------

def test_census_returns_lng_lat():
    get = lambda url, params, timeout: R(census_body(-96.8, 32.78))
    assert mp.census_geocode("1 Main St, Dallas, TX 75201", get) == (-96.8, 32.78)


def test_census_miss_fails_closed():
    get = lambda url, params, timeout: R({"result": {"addressMatches": []}})
    assert mp.census_geocode("nowhere", get) is None


def test_census_rejects_contradicting_state_or_zip():
    get = lambda url, params, timeout: R(census_body(-119.7, 36.7, "CA", "93650"))
    assert mp.census_geocode("1 Main St, Dallas, TX 75201", get) is None
    get = lambda url, params, timeout: R(census_body(-96.8, 32.78, "TX", "75202"))
    assert mp.census_geocode("1 Main St, Dallas, TX 75201", get) is None


def test_ors_geocode_accepts_only_address_level(monkeypatch):
    def body(layer, conf=0.95):
        return {"features": [{"geometry": {"coordinates": [-96.8, 32.78]},
                              "properties": {"layer": layer, "confidence": conf,
                                             "region_a": "TX", "postalcode": "75201"}}]}
    for layer, ok in (("address", True), ("venue", True), ("street", False), ("locality", False)):
        get = lambda url, params, timeout, b=body(layer): R(b)
        assert (mp.ors_geocode("1 Main St, Dallas, TX", "k", get) is not None) is ok
    get = lambda url, params, timeout: R(body("address", 0.5))
    assert mp.ors_geocode("1 Main St, Dallas, TX", "k", get) is None


def test_free_geocode_falls_back_to_ors(monkeypatch):
    monkeypatch.setenv("ORS_API_KEY", "k")
    def get(url, params, timeout):
        if "census" in url:
            return R({"result": {"addressMatches": []}})
        return R({"features": [{"geometry": {"coordinates": [-96.8, 32.78]},
                                "properties": {"layer": "venue", "confidence": 1}}]})
    assert mp.free_geocode("Kroger Dallas TX", get) == (-96.8, 32.78)


# --- routing --------------------------------------------------------------

class FakeORS:
    def __init__(self, order=(2, 0, 3, 1), fail=False):
        self.order, self.fail, self.calls = order, fail, []

    def __call__(self, url, json, headers, timeout):
        assert headers["Authorization"] == "k"
        self.calls.append((url, json))
        if self.fail:
            return R({"error": "quota"})
        if url.endswith("/optimization"):
            assert json["vehicles"][0]["start"] == json["vehicles"][0]["end"]
            steps = ([{"type": "start"}]
                     + [{"type": "job", "job": i + 1} for i in self.order[:len(json["jobs"])]]
                     + [{"type": "end"}])
            return R({"code": 0, "unassigned": [], "routes": [{"steps": steps}]})
        assert len(json["coordinates"]) <= mp.ORS_MAX_DIRECTION_POINTS
        n = len(json["coordinates"]) - 1
        return R({"routes": [{"summary": {"distance": 1609.344 * n, "duration": 3600 * n}}]})


def test_ors_round_trip_order_and_totals():
    post = FakeORS()
    miles, hours, order = mp.plan_round_trip_free(HOME, STOPS, key="k", request_post=post)
    assert order == [2, 0, 3, 1]
    assert round(miles, 3) == 5 and round(hours, 3) == 5  # 5 legs home->4 stops->home
    measured = post.calls[-1][1]["coordinates"]
    assert measured[0] == measured[-1] == list(HOME)
    assert len(post.calls) == 2  # one optimization + one directions call


def test_large_route_uses_seed_and_chunked_directions():
    stops = [(-96.8 + i * 0.01, 32.7 + (i % 7) * 0.01) for i in range(120)]
    post = FakeORS()
    miles, hours, order = mp.plan_round_trip_free(
        HOME, stops, key="k", request_post=post, seed_order=geographic_order)
    assert sorted(order) == list(range(120))
    assert not any(u.endswith("/optimization") for u, _ in post.calls)
    assert round(miles, 3) == 121  # chunks share endpoints: no gaps, no double count


def test_ors_failure_falls_back_to_osrm():
    def get(url, params, timeout):
        assert "/trip/v1/driving/" in url and params["source"] == "first"
        wps = [{"waypoint_index": 0}] + [{"waypoint_index": r} for r in (3, 1, 4, 2)]
        return R({"code": "Ok", "waypoints": wps,
                  "trips": [{"distance": 16093.44, "duration": 7200}]})
    miles, hours, order = mp.plan_round_trip_free(
        HOME, STOPS, key="k", request_post=FakeORS(fail=True), request_get=get)
    assert order == [1, 3, 0, 2]
    assert round(miles, 2) == 10 and hours == 2


def test_no_key_goes_straight_to_osrm(monkeypatch):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    post = FakeORS()
    get = lambda url, params, timeout: R({"code": "Ok",
        "waypoints": [{"waypoint_index": i} for i in range(5)],
        "trips": [{"distance": 0, "duration": 0}]})
    assert mp.plan_round_trip_free(HOME, STOPS, request_post=post, request_get=get)[2] == [0, 1, 2, 3]
    assert post.calls == []


def test_provider_switch_defaults_to_mapbox(monkeypatch):
    monkeypatch.delenv("MAP_PROVIDER", raising=False)
    assert not mp.use_free()
    monkeypatch.setenv("MAP_PROVIDER", "Free")
    assert mp.use_free()


# --- persistence ----------------------------------------------------------

def test_persistent_cache_survives_new_instance(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/c.db")
    cache = mp.PersistentMapCache(engine)
    assert cache.ready
    cache.save_geocode("1 Main St", (-96.8, 32.78), "census")
    cache.save_geocode("1 Main St", (-96.81, 32.79), "census")  # upsert
    key = ("continuous-v3", "home", ("a", "b"), (None, None))
    cache.save_route(key, (12.3, 1.5, "1h 30m", [1, 0]), 1_000_000_000.0)

    again = mp.PersistentMapCache(engine)  # e.g. after a redeploy
    assert again.load_geocodes() == {"1 Main St": (-96.81, 32.79)}
    result, saved = again.get_route(key, max_age=10**12)
    assert result == (12.3, 1.5, "1h 30m", [1, 0])
    assert again.get_route(key, max_age=1) is None  # expired


def test_persistent_cache_without_db_is_noop():
    cache = mp.PersistentMapCache(None)
    cache.save_geocode("x", (1, 2), "s")
    assert cache.load_geocodes() == {} and cache.get_route("k") is None


def test_get_gmaps_free_mode_uses_ors_and_persists_route(tmp_path):
    """get_gmaps in MAP_PROVIDER=free: no Mapbox token needed, route saved to
    Postgres and served from it after a 'redeploy' (fresh in-memory cache)."""
    import ast
    import time
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from types import SimpleNamespace
    from route_planning import task_stop_locations
    node = next(n for n in ast.parse(Path('tactical_workspace_master_rw.py').read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'get_gmaps')
    geocodes = {'home': HOME, **{f's{i}': s for i, s in enumerate(STOPS)}}
    persist = mp.PersistentMapCache(sa.create_engine(f"sqlite:///{tmp_path}/r.db"))
    post = FakeORS()

    def build():
        scope = {'time': time, 'ThreadPoolExecutor': ThreadPoolExecutor, 'MAPBOX_TOKEN': None,
                 'USE_FREE_MAPS': True, '_map_persist': lambda: persist,
                 '_gmaps_route_cache': (lambda c={}: c), '_mapbox_geocode_cache': lambda: geocodes,
                 '_mapbox_geocode': lambda a, **k: geocodes[a], 'requests': None,
                 '_log_err': lambda *a: print(a), 'task_stop_locations': task_stop_locations,
                 'plan_round_trip': None, 'geographic_order': geographic_order,
                 '_mp': SimpleNamespace(plan_round_trip_free=lambda h, s, **k:
                                        mp.plan_round_trip_free(h, s, key='k', request_post=post, **k))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'routing', 'exec'), scope)
        return scope['get_gmaps']

    stops = ('s0', 's1', 's2', 's3')
    first = build()('home', stops)
    assert first[0] == 5 and first[3] == [2, 0, 3, 1]
    calls = len(post.calls)
    after_redeploy = build()('home', stops)  # new process-wide cache, same DB
    assert tuple(after_redeploy) == tuple(first)
    assert len(post.calls) == calls  # no new API calls
