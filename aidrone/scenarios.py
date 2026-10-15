"""Scenario library: urban deliveries across weather, faults and attacks."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .citymap import BoxObstacle, CityMap
from .config import PROFILES, VehicleProfile
from .fleet import DeliveryOrder, Dispatcher
from .geometry import norm, unit, vec
from .planning import GlobalPlanner
from .security import MarkerClaims, MarkerSigner, MarkerVerifier, new_nonce
from .sim.runner import SimResult, Simulation
from .sim.world import MovingObject, Person, World
from .weather import WeatherState, WeatherTimeline

CLEAR = WeatherState(wind_speed=3.0, wind_from_deg=250, gust_std=0.6)


@dataclass
class Scenario:
    name: str
    description: str
    site_id: str
    weather: WeatherTimeline
    expect_delivery: bool | None = True  # None = either outcome acceptable if safe
    payload_kg: float = 2.5
    start_soc: float = 1.0
    profile: str = "urban_courier"
    rtk: bool = True
    setup: Callable[["Scenario", World, CityMap, Dispatcher, dict], None] | None = None
    forecast: WeatherTimeline | None = None  # what dispatch believed (None = perfect forecast)


def _route(city: CityMap, profile: VehicleProfile, site_id: str) -> list[np.ndarray]:
    planner = GlobalPlanner(city, profile)
    planner.set_clearance(profile.safety_radius_m + 4.0)
    site = city.sites[site_id]
    alt = profile.cruise_altitude_agl
    res = planner.plan(vec(*city.depot.position[:2], alt), vec(site.position[0], site.position[1], max(alt, site.position[2] + 25)))
    return res.waypoints


def _point_along(pts: list[np.ndarray], frac: float) -> tuple[np.ndarray, np.ndarray]:
    lengths = [norm(b - a) for a, b in zip(pts, pts[1:])]
    target = frac * sum(lengths)
    for (a, b), L in zip(zip(pts, pts[1:]), lengths):
        if target <= L:
            return a + unit(b - a) * target, unit(b - a)
        target -= L
    return pts[-1], unit(pts[-1] - pts[-2])


# ----------------------------------------------------------------- setups
def _crane(sc, world, hd_map, dispatcher, ctx):
    pts = _route(world.city, ctx["profile"], sc.site_id)
    p, d = _point_along(pts, 0.45)
    perp = vec(-d[1], d[0], 0)
    half = np.abs(perp) * 25.0 + np.abs(d) * 2.0
    lo = vec(p[0], p[1], p[2] - 5.0) - half
    hi = vec(p[0], p[1], p[2] + 3.0) + half
    world.city.obstacles.append(BoxObstacle("tower_crane_jib", lo, hi, mapped=False))
    mast = vec(p[0], p[1], 0) + perp * 22.0
    world.city.obstacles.append(BoxObstacle("tower_crane_mast", mast - vec(1.5, 1.5, 0), mast + vec(1.5, 1.5, p[2] + 3), mapped=False))


def _traffic(sc, world, hd_map, dispatcher, ctx):
    prof = ctx["profile"]
    pts = _route(world.city, prof, sc.site_id)
    p_meet, d = _point_along(pts, 0.5)
    t_meet = prof.cruise_altitude_agl / (prof.max_climb_rate * 0.7) + 0.5 * sum(norm(b - a) for a, b in zip(pts, pts[1:])) / prof.cruise_speed
    v = -d * 10.0  # head-on, same altitude: the worst geometry
    world.movers.append(MovingObject("uncooperative_drone", p_meet - v * t_meet, v, radius=1.0, label="drone"))
    world.movers.append(MovingObject("bird_flock", p_meet + vec(0, 0, -4) - vec(4, -3, 0) * (t_meet + 8), vec(4, -3, 0),
                                     radius=1.5, label="bird"))


def _person_on_pad(sc, world, hd_map, dispatcher, ctx):
    site = world.city.sites[sc.site_id]
    world.people.append(Person(site.position + vec(1.0, 0.5, 0.0), t_leave=ctx["arrival_estimate"] + 25.0))


def _spoof(sc, world, hd_map, dispatcher, ctx):
    attacker = MarkerSigner()  # not the dispatch key
    site = world.city.sites[sc.site_id]
    claims = MarkerClaims(ctx["order"].order_id, site.site_id, tuple(site.position), 1e9, new_nonce())
    world.marker_payloads[sc.site_id] = attacker.sign(claims)


def _relocated(sc, world, hd_map, dispatcher, ctx):
    site = world.city.sites[sc.site_id]
    world.marker_positions[sc.site_id] = site.position + vec(6.0, -4.0, 0.0)  # thief moved the genuine marker


def _motor_failure(sc, world, hd_map, dispatcher, ctx):
    world.motor_failures.append(70.0)


def _link_loss(sc, world, hd_map, dispatcher, ctx):
    world.link_outages.append((45.0, 110.0))


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("clear_day", "Baseline park delivery, light breeze", "park_pad", WeatherTimeline(CLEAR)),
        Scenario(
            "urban_storm",
            "Heavy rain 30 mm/h, 8 m/s surface wind (~14 m/s aloft) with gusts: rain clutter, false rangefinder hits",
            "rooftop_pad",
            WeatherTimeline(WeatherState(wind_speed=8.0, wind_from_deg=230, gust_std=1.4, rain_mm_h=30.0,
                                         visibility_m=1800, temperature_c=12, humidity=0.98, illumination_lux=3000)),
        ),
        Scenario(
            "night_fog",
            "Night, fog (180 m visibility): thermal + NIR + radar, tethered drop into a courtyard",
            "courtyard_winch",
            WeatherTimeline(WeatherState(wind_speed=2.0, gust_std=0.4, visibility_m=180, temperature_c=6,
                                         humidity=0.99, illumination_lux=0.5)),
        ),
        Scenario(
            "winter_snow",
            "-8 C, snow 4 mm/h: icing protection, cold battery derating",
            "park_pad",
            WeatherTimeline(WeatherState(wind_speed=6.0, wind_from_deg=10, gust_std=1.0, snow_mm_h=4.0,
                                         visibility_m=1200, temperature_c=-8, humidity=0.9, illumination_lux=4000)),
        ),
        Scenario(
            "gnss_denied_canyon",
            "No RTK corrections; winch drop deep in a courtyard where GNSS multipath/NLOS is severe",
            "courtyard_winch",
            WeatherTimeline(WeatherState(wind_speed=7.0, wind_from_deg=300, gust_std=1.2)),
            rtk=False,
        ),
        Scenario("crane_obstacle", "Unmapped tower crane across the planned route", "park_pad", WeatherTimeline(CLEAR),
                 setup=_crane),
        Scenario("drone_traffic", "Uncooperative drone head-on at cruise altitude plus a bird flock", "park_pad",
                 WeatherTimeline(CLEAR), setup=_traffic),
        Scenario("person_on_pad", "Someone is standing on the pad and leaves after a while", "park_pad",
                 WeatherTimeline(CLEAR), setup=_person_on_pad),
        Scenario("spoofed_marker", "Counterfeit landing marker (signed with the wrong key)", "park_pad",
                 WeatherTimeline(CLEAR), expect_delivery=False, setup=_spoof),
        Scenario("relocated_marker", "Genuine marker moved 7 m by a thief", "park_pad", WeatherTimeline(CLEAR),
                 expect_delivery=False, setup=_relocated),
        Scenario(
            "thunderstorm_inbound",
            "Pop-up storm the forecast missed: nowcast shows lightning mid-flight -> abort, then land at nearest safe site",
            "rooftop_pad",
            WeatherTimeline([
                (0.0, WeatherState(wind_speed=4.0, gust_std=0.8, lightning_km=60.0)),
                (40.0, WeatherState(wind_speed=5.0, gust_std=1.0, lightning_km=13.0, rain_mm_h=5)),
                (75.0, WeatherState(wind_speed=7.0, gust_std=2.0, lightning_km=5.0, rain_mm_h=25)),
            ]),
            expect_delivery=False,
            forecast=WeatherTimeline(WeatherState(wind_speed=4.0, gust_std=0.8)),
        ),
        Scenario(
            "hurricane_no_go",
            "Sustained 22 m/s winds: the safest delivery is the one that waits",
            "park_pad",
            WeatherTimeline(WeatherState(wind_speed=22.0, gust_std=4.0, rain_mm_h=20)),
            expect_delivery=False,
        ),
        Scenario(
            "freezing_rain_no_go",
            "Freezing rain: ice accretion beyond de-icing capacity",
            "park_pad",
            WeatherTimeline(WeatherState(wind_speed=4.0, temperature_c=-1.0, rain_mm_h=3.0, freezing_rain=True)),
            expect_delivery=False,
        ),
        Scenario("motor_failure", "One of six motors fails in cruise", "park_pad", WeatherTimeline(CLEAR),
                 expect_delivery=None, setup=_motor_failure),
        Scenario("link_loss", "C2 link lost for 65 s mid-flight: aircraft continues autonomously", "park_pad",
                 WeatherTimeline(CLEAR), setup=_link_loss),
        Scenario(
            "low_battery_headwind",
            "Battery at 30 % with a headwind home: energy-margin check refuses the mission",
            "park_pad",
            WeatherTimeline(WeatherState(wind_speed=6.0, wind_from_deg=225, gust_std=1.0)),
            start_soc=0.30,
            expect_delivery=False,
        ),
    ]
}


def build(name: str, seed: int = 0, city_seed: int = 7) -> Simulation:
    sc = SCENARIOS[name]
    profile = PROFILES[sc.profile]
    hd_map = CityMap.generate(seed=city_seed)
    truth = copy.deepcopy(hd_map)
    dispatcher = Dispatcher(hd_map)
    order = DeliveryOrder(f"ORD-{name.upper()}-{seed}", sc.site_id, sc.payload_kg)
    ticket = dispatcher.create_ticket(order, now=0.0)
    world = World(truth, sc.weather, seed=seed, rtk_available=sc.rtk)
    world.marker_payloads[sc.site_id] = ticket.marker_payload
    route = _route(truth, profile, sc.site_id)
    arrival = profile.cruise_altitude_agl / (profile.max_climb_rate * 0.7) + sum(
        norm(b - a) for a, b in zip(route, route[1:])) / profile.cruise_speed + 15.0
    ctx = {"profile": profile, "order": order, "arrival_estimate": arrival}
    if sc.setup:
        sc.setup(sc, world, hd_map, dispatcher, ctx)
    verifier = MarkerVerifier(dispatcher.public_key)

    def recipient(t: float) -> str:
        return ticket.recipient_code()

    return Simulation(world, hd_map, profile, ticket, verifier, name=name, recipient_code_fn=recipient, seed=seed,
                      start_soc=sc.start_soc, forecast=sc.forecast)


def run(name: str, seed: int = 0) -> SimResult:
    return build(name, seed).run()


def evaluate(result: SimResult) -> tuple[bool, str]:
    """Pass/fail against the scenario's expectation. Safety is non-negotiable."""
    sc = SCENARIOS[result.scenario]
    if not result.safe_outcome:
        return False, "unsafe outcome (collision or hard impact)"
    if sc.expect_delivery is True and not result.delivered:
        return False, f"expected delivery, got: {result.abort_reason}"
    if sc.expect_delivery is False and result.delivered:
        return False, "delivered when it should have refused/aborted"
    if result.delivered and result.delivery_error_m is not None and result.delivery_error_m > 0.5:
        return False, f"landing error {result.delivery_error_m:.2f} m too large"
    return True, "ok"
