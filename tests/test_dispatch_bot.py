import math
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dispatch_bot as bot  # noqa: E402


def haversine(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


MILE = 1 / 69.0  # ~1 mile of latitude


def route(n_stops, lat=30.0, lon=-97.0, city="Austin"):
    data = [{"id": f"t{i}", "full": f"{i} Main St", "lat": lat, "lon": lon} for i in range(n_stops)]
    return {"data": data, "stops": n_stops, "city": city, "state": "TX", "center": [lat, lon]}


def center(r):
    return (r["data"][0]["lat"], r["data"][0]["lon"])


def ic_df(rows):
    return pd.DataFrame([{"id": i, "name": n, "ic list": s, "lat": 30.0 + miles * MILE,
                          "lng": -97.0, "phone": ph}
                         for i, (n, s, miles, ph) in enumerate(rows)])


def plan(entries, df, counts=None, waiting=None, hours=1.0, mode="routes",
         declined_by=None, **settings):
    ics = bot.candidate_ics(df)
    return bot.plan_routes(entries, ics, haversine, center, lambda ic, r: hours,
                           counts if counts is not None else {}, waiting or {},
                           lambda row: f"id:{row['id']}", bot.BotSettings(**settings),
                           mode=mode, declined_by=declined_by)


class RateRule(unittest.TestCase):
    def test_small_routes_kept_between_20_and_24_50(self):
        self.assertEqual(bot.rate_for_route(3, 12.0), 20.0)
        self.assertEqual(bot.rate_for_route(3, 22.37), 22.37)
        self.assertEqual(bot.rate_for_route(5, 31.0), 24.5)
        self.assertEqual(bot.rate_for_route(1, 0), 20.0)

    def test_six_or_more_stops_flat_20(self):
        self.assertEqual(bot.rate_for_route(6, 40.0), 20.0)
        self.assertEqual(bot.rate_for_route(12, 10.0), 20.0)


class Planning(unittest.TestCase):
    def test_small_route_far_from_ics_goes_to_field_nation(self):
        df = ic_df([("Far Fran", "ACTIVE", 30, "5125550001")])
        p, = plan([("Red", route(3), "Ready", "h1", None)], df)
        self.assertEqual(p.action, "fn")

    def test_large_route_same_distance_stays_with_ic(self):
        df = ic_df([("Far Fran", "ACTIVE", 30, "5125550001")])
        p, = plan([("Red", route(8), "Ready", "h1", None)], df, counts={"5125550001": 3})
        self.assertEqual(p.action, "ic")
        self.assertEqual(p.rate, 20.0)
        self.assertEqual(p.pay, 160.0)

    def test_closest_ic_wins_over_far_new_ic(self):
        df = ic_df([("Close Cal", "ACTIVE", 2, "1"), ("New Nia", "IN TRAINING", 20, "2")])
        p, = plan([("Red", route(4), "Ready", "h1", None)], df, counts={"1": 4, "2": 0})
        self.assertEqual(p.ic_name, "Close Cal")
        self.assertFalse(p.is_new_ic)

    def test_new_ic_preferred_when_nearly_as_close(self):
        df = ic_df([("Close Cal", "ACTIVE", 2, "1"), ("New Nia", "IN TRAINING", 5, "2")])
        p, = plan([("Red", route(4), "Ready", "h1", None)], df, counts={"1": 4, "2": 0})
        self.assertEqual(p.ic_name, "New Nia")
        self.assertTrue(p.is_new_ic)
        self.assertEqual(p.ic_identity, "id:1")

    def test_overloaded_ic_skipped(self):
        df = ic_df([("Busy Bo", "ACTIVE", 1, "1"), ("Next Ned", "ACTIVE", 8, "2")])
        p, = plan([("Red", route(4), "Ready", "h1", None)], df,
                  counts={"1": 40, "2": 3})
        self.assertEqual(p.ic_name, "Next Ned")

    def test_waiting_route_requests_count_as_load_and_spread_within_run(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 3, "2")])
        entries = [("Red", route(4, city=c), "Ready", f"h{i}", None) for i, c in enumerate("ABC")]
        out = plan(entries, df, counts={"1": 2, "2": 2}, waiting={"ann": 1})
        names = [p.ic_name for p in out if p.action == "ic"]
        self.assertEqual(names.count("Ann"), 1)  # 1 waiting + 1 new = cap of 2
        self.assertEqual(names.count("Ben"), 2)
        self.assertEqual(len(names), 3)

    def test_calculated_rate_is_clamped_for_small_route(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1")])
        p, = plan([("Red", route(2), "Ready", "h1", None)], df, counts={"1": 1}, hours=3.0)
        self.assertEqual(p.calc_rate, 37.5)
        self.assertEqual(p.rate, 24.5)
        self.assertEqual(p.pay, 49.0)

    def test_inactive_and_field_agents_ignored_and_digital_skipped(self):
        df = ic_df([("Old Ollie", "INACTIVE", 1, "1")])
        p, = plan([("Red", route(8), "Ready", "h1", None)], df)
        self.assertEqual(p.action, "skip")
        d, = plan([("Global_Digital", route(2), "Ready", "h2", None)], ic_df([("Ann", "ACTIVE", 1, "1")]))
        self.assertEqual(d.action, "skip")

    def test_sent_routes_not_touched(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1")])
        self.assertEqual(plan([("Red", route(3), "Sent", "h1", None)], df), [])

    def test_waiting_counts_from_sent_db(self):
        sent_db = {"t1": {"name": "Ann", "status": "sent", "wo": "Ann-1"},
                   "t2": {"name": "Ann", "status": "sent", "wo": "Ann-1"},
                   "t3": {"name": "Ann", "status": "accepted", "wo": "Ann-2"}}
        self.assertEqual(bot.waiting_routes_by_ic(sent_db), {"ann": 1})



class Reoffer(unittest.TestCase):
    def test_next_closest_ic_skipping_decliners(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 10, "2"), ("Cy", "ACTIVE", 20, "3")])
        p, = plan([("Red", route(3), "Declined", "h1", None)], df, counts={"1": 1, "2": 1, "3": 1},
                  mode="reoffer", declined_by={"Red:h1": {"ann", "ben"}})
        self.assertEqual((p.action, p.ic_name), ("ic", "Cy"))
        self.assertIn("2 IC(s) who declined", p.reason)

    def test_small_route_reoffer_goes_past_25_miles(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Far Fay", "ACTIVE", 35, "2")])
        p, = plan([("Red", route(2), "Declined", "h1", None)], df, counts={"1": 1, "2": 1},
                  mode="reoffer", declined_by={"Red:h1": {"ann"}})
        self.assertEqual((p.action, p.ic_name), ("ic", "Far Fay"))

    def test_field_nation_when_nobody_left_within_40(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Too Far", "ACTIVE", 45, "2")])
        p, = plan([("Red", route(8), "Declined", "h1", None)], df, counts={"1": 1, "2": 1},
                  mode="reoffer", declined_by={"Red:h1": {"ann"}})
        self.assertEqual(p.action, "fn")

    def test_reoffer_only_looks_at_declined_routes(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1")])
        self.assertEqual(plan([("Red", route(3), "Ready", "h1", None)], df, mode="reoffer"), [])

    def test_routes_mode_never_picks_a_past_decliner(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 4, "2")])
        p, = plan([("Red", route(8), "Ready", "h1", None)], df, counts={"1": 1, "2": 1},
                  declined_by={"Red:h1": {"ann"}})
        self.assertEqual(p.ic_name, "Ben")

    def test_decliners_from_sent_db_and_history(self):
        r = route(2)
        r["data"][0]["id"], r["data"][1]["id"] = "t1", "t2"
        sent_db = {"t1": {"name": "Ann", "status": "declined", "wo": "Ann-1"},
                   "t2": {"name": "Zed", "status": "sent", "wo": "Zed-1"}}
        got = bot.decliners_by_key([("Red", r, "Declined", "h1", None)], sent_db,
                                   [("Ben", ["t2", "x9"]), ("Old Ole", ["other"])])
        self.assertEqual(got, {"Red:h1": {"ann", "ben"}})


class Removals(unittest.TestCase):
    def test_spread_across_ics_before_doubling_up(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 12, "2")])
        entries = [("Red", route(1, city=c), "Ready", f"h{i}", None) for i, c in enumerate("ABC")]
        out = plan(entries, df, mode="removals")
        names = sorted(p.ic_name for p in out)
        self.assertEqual(names, ["Ann", "Ann", "Ben"])  # third one doubles only after both have one

    def test_waiting_route_request_counts_as_having_one(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 12, "2")])
        p, = plan([("Red", route(1), "Ready", "h1", None)], df, mode="removals", waiting={"ann": 1})
        self.assertEqual(p.ic_name, "Ben")

    def test_ignores_load_limits_and_new_ic_rule(self):
        df = ic_df([("Busy Bo", "ACTIVE", 1, "1"), ("New Nia", "IN TRAINING", 3, "2")])
        p, = plan([("Red", route(1), "Ready", "h1", None)], df, mode="removals",
                  counts={"1": 99, "2": 0})
        self.assertEqual(p.ic_name, "Busy Bo")

    def test_field_nation_past_30_miles(self):
        df = ic_df([("Far Fran", "ACTIVE", 31, "1")])
        p, = plan([("Red", route(1), "Ready", "h1", None)], df, mode="removals")
        self.assertEqual(p.action, "fn")

    def test_declined_removal_skips_decliner(self):
        df = ic_df([("Ann", "ACTIVE", 1, "1"), ("Ben", "ACTIVE", 12, "2")])
        p, = plan([("Red", route(1), "Ready", "h1", None)], df, mode="removals",
                  declined_by={"Red:h1": {"ann"}})
        self.assertEqual(p.ic_name, "Ben")


if __name__ == "__main__":
    unittest.main()
