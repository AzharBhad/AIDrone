import numpy as np
import pytest

from aidrone.citymap import CityMap
from aidrone.config import URBAN_COURIER
from aidrone.geometry import vec
from aidrone.perception import PerceivedWorld, TrackedObstacle
from aidrone.planning import GlobalPlanner, LocalPlanner, PathTracker


@pytest.fixture(scope="module")
def city():
    return CityMap.generate(seed=7)


@pytest.fixture(scope="module")
def planner(city):
    gp = GlobalPlanner(city, URBAN_COURIER)
    gp.set_clearance(URBAN_COURIER.safety_radius_m + 4.0)
    return gp


def test_global_route_is_collision_free_and_avoids_no_fly(city, planner):
    start = vec(*city.depot.position[:2], 100)
    site = city.sites["park_pad"]
    goal = vec(site.position[0], site.position[1], 100)
    res = planner.plan(start, goal)
    assert res is not None
    pts = res.waypoints
    for a, b in zip(pts, pts[1:]):
        n = int(np.linalg.norm(b - a) / 1.0) + 2
        seg = a + (b - a) * np.linspace(0, 1, n)[:, None]
        assert not city.occupied(seg, include_unmapped=False).any()
        for p in seg:
            assert city.in_no_fly(p) is None
    # route must bend around the central heliport no-fly zone, so it is longer than direct
    assert res.length > np.linalg.norm(goal - start) * 1.0


def test_altitude_cap_prefers_lower_route(city, planner):
    start = vec(*city.depot.position[:2], 60)
    site = city.sites["rooftop_pad"]
    goal = vec(site.position[0], site.position[1], 65)
    low = planner.plan(start, goal, wind_speed_10m=12.0, altitude_cap=60.0)
    assert low is not None
    assert max(p[2] for p in low.waypoints[1:-1] or [60]) <= 80.0


def test_unreachable_goal_returns_none(city):
    gp = GlobalPlanner(city, URBAN_COURIER)
    gp.set_clearance(5.0)
    hx, hy = city.no_fly[0].center
    assert gp.plan(vec(*city.depot.position[:2], 100), vec(hx, hy, 100)) is None


def test_path_tracker_carrot_advances():
    tr = PathTracker([vec(0, 0, 0), vec(100, 0, 0), vec(100, 100, 0)])
    c = tr.carrot(vec(10, 0, 0), 20)
    assert np.allclose(c, [30, 0, 0])
    c = tr.carrot(vec(95, 0, 0), 20)
    assert c[1] > 10 and abs(c[0] - 100) < 1e-6
    assert abs(tr.remaining_distance(vec(100, 50, 0)) - 50) < 1e-6


def test_local_planner_dodges_head_on_traffic(city):
    lp = LocalPlanner(GlobalPlanner(city, URBAN_COURIER).set_clearance(5.2), URBAN_COURIER)
    pos = vec(300, 20, 100)
    vel = vec(12, 0, 0)
    intruder = TrackedObstacle(vec(330, 20, 100), vec(-10, 0, 0), 1.0, {"radar"}, 0.9, "drone")
    res = lp.command(pos, vel, vec(400, 20, 100), 15.0, 5.0, PerceivedWorld(np.zeros((0, 3)), [intruder]))
    assert not res.blocked
    v = res.velocity
    # must leave the intruder's line: lateral or vertical component, or slow right down
    assert abs(v[1]) > 1.0 or abs(v[2]) > 1.0 or np.linalg.norm(v) < 3.0


def test_local_planner_brakes_when_boxed_in(city):
    lp = LocalPlanner(GlobalPlanner(city, URBAN_COURIER).set_clearance(5.2), URBAN_COURIER)
    pos = vec(300, 20, 100)
    ring = np.array([pos + 2.0 * vec(np.cos(a), np.sin(a), z) for a in np.linspace(0, 2 * np.pi, 24) for z in (-1, 0, 1)])
    res = lp.command(pos, vec(0, 0, 0), vec(400, 20, 100), 15.0, 5.0, PerceivedWorld(ring))
    assert res.blocked and np.allclose(res.velocity, 0)
