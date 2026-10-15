"""Hierarchical planning (doc 1.5): risk-aware 3D A* + reactive DWA/VO.

Global planner
    3D A* over a coarse grid derived from the HD map. Edge cost blends
    distance with ground risk (population density under the flight path -
    fly over rooftops and parks, not crowded streets), wind exposure (wind
    grows with altitude; strong wind pushes the route lower) and a cruise
    altitude preference. Hard constraints: inflated buildings, no-fly zones,
    geofence, obstacles discovered in flight. The obstacle inflation grows
    with gusts and navigation uncertainty.

Local planner
    Dynamic-window sampling of velocity commands, each rolled out with the
    vehicle's acceleration limit (reduced by wind load) and checked against
    the HD map, confirmed perception voxels and predicted positions of moving
    objects/peer drones (velocity obstacles). Speed is capped so the vehicle
    can always stop inside its sensed free space.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np

from .citymap import CityMap
from .config import VehicleProfile
from .geometry import clamp_norm, norm, unit, vec
from .perception import PerceivedWorld
from .weather import wind_at_altitude


def _max_filter2d(a: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return a.copy()
    out = a.copy()
    for axis in (0, 1):
        src = out.copy()
        padded = np.pad(src, [(r, r) if i == axis else (0, 0) for i in range(2)], mode="edge")
        for s in range(2 * r + 1):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(s, s + a.shape[axis])
            out = np.maximum(out, padded[tuple(sl)])
    return out


class InflatedMap:
    """HD map heights inflated by a lateral and vertical safety clearance."""

    def __init__(self, hd_map: CityMap, lateral: float, vertical: float) -> None:
        self.map = hd_map
        self.lateral = lateral
        self.vertical = vertical
        r = int(math.ceil(lateral / hd_map.res))
        self.heights = _max_filter2d(hd_map.heights, r) + vertical
        self.extra: list[tuple[np.ndarray, np.ndarray]] = []  # inflated boxes from perception

    def add_box(self, lo: np.ndarray, hi: np.ndarray) -> None:
        pad = np.full(3, self.lateral)
        self.extra.append((lo - pad, hi + pad))

    def blocked(self, pts: np.ndarray, nofly_margin: float = 10.0) -> np.ndarray:
        pts = np.atleast_2d(pts)
        m = self.map
        ix = np.clip((pts[:, 0] / m.res).astype(int), 0, m.nx - 1)
        iy = np.clip((pts[:, 1] / m.res).astype(int), 0, m.ny - 1)
        out = pts[:, 2] < self.heights[ix, iy]
        out |= (pts[:, 0] < 5) | (pts[:, 1] < 5) | (pts[:, 0] > m.size_x - 5) | (pts[:, 1] > m.size_y - 5)
        for z in m.no_fly:
            d = np.hypot(pts[:, 0] - z.center[0], pts[:, 1] - z.center[1])
            out |= (d < z.radius + nofly_margin) & (pts[:, 2] >= z.z_min) & (pts[:, 2] <= z.z_max)
        for lo, hi in self.extra:
            out |= np.all((pts >= lo) & (pts <= hi), axis=1)
        return out

    def segment_free(self, a: np.ndarray, b: np.ndarray, step: float = 2.0) -> bool:
        n = max(2, int(norm(b - a) / step) + 1)
        pts = a[None, :] + (b - a)[None, :] * np.linspace(0, 1, n)[:, None]
        return not bool(self.blocked(pts).any())


@dataclass
class PlanResult:
    waypoints: list[np.ndarray]
    cost: float
    expanded: int
    altitude_cap_used: float | None

    @property
    def length(self) -> float:
        return float(sum(norm(b - a) for a, b in zip(self.waypoints, self.waypoints[1:])))


class GlobalPlanner:
    def __init__(
        self,
        hd_map: CityMap,
        profile: VehicleProfile,
        xy_res: float = 8.0,
        z_res: float = 8.0,
        w_population: float = 3.0,
        w_wind: float = 1.5,
        w_altitude: float = 0.01,
        epsilon: float = 1.4,
    ) -> None:
        self.map = hd_map
        self.profile = profile
        self.xy_res = xy_res
        self.z_res = z_res
        self.w_pop = w_population
        self.w_wind = w_wind
        self.w_alt = w_altitude
        self.eps = epsilon
        self.nx = int(hd_map.size_x / xy_res)
        self.ny = int(hd_map.size_y / xy_res)
        self.levels = np.arange(z_res, profile.max_altitude_agl + 1e-6, z_res)
        k = int(round(xy_res / hd_map.res))
        pop = hd_map.population[: self.nx * k, : self.ny * k]
        self.pop_c = pop.reshape(self.nx, k, self.ny, k).mean(axis=(1, 3))
        self.inflated: InflatedMap | None = None

    def set_clearance(self, lateral: float, vertical: float = 4.0) -> InflatedMap:
        prev = self.inflated.extra if self.inflated is not None else []
        self.inflated = InflatedMap(self.map, lateral, vertical)
        self.inflated.extra = list(prev)
        k = int(round(self.xy_res / self.map.res))
        h = self.inflated.heights[: self.nx * k, : self.ny * k]
        self.h_c = h.reshape(self.nx, k, self.ny, k).max(axis=(1, 3))
        return self.inflated

    def _node_free(self, i: int, j: int, kz: int) -> bool:
        assert self.inflated is not None
        c = self._center(i, j, kz)
        return bool(c[2] > self.h_c[i, j]) and not bool(self.inflated.blocked(c[None, :])[0])

    def _center(self, i: int, j: int, kz: int) -> np.ndarray:
        return vec((i + 0.5) * self.xy_res, (j + 0.5) * self.xy_res, self.levels[kz])

    def _snap(self, p: np.ndarray, kmax: int) -> tuple[int, int, int] | None:
        i = int(np.clip(p[0] // self.xy_res, 0, self.nx - 1))
        j = int(np.clip(p[1] // self.xy_res, 0, self.ny - 1))
        k0 = int(np.clip(np.argmin(np.abs(self.levels - p[2])), 0, kmax))
        for k in list(range(k0, kmax + 1)) + list(range(k0 - 1, -1, -1)):
            if self._node_free(i, j, k):
                return i, j, k
        for r in range(1, 4):  # search neighbouring columns
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < self.nx and 0 <= jj < self.ny:
                        for k in range(k0, kmax + 1):
                            if self._node_free(ii, jj, k):
                                return ii, jj, k
        return None

    def plan(
        self,
        start: np.ndarray,
        goal: np.ndarray,
        wind_speed_10m: float = 0.0,
        altitude_cap: float | None = None,
        max_expansions: int = 400_000,
    ) -> PlanResult | None:
        if self.inflated is None:
            self.set_clearance(self.profile.safety_radius_m + 4.0)
        caps = [altitude_cap] if altitude_cap is not None else []
        caps += [None]  # soft constraint: relax the weather altitude cap if it makes the goal unreachable
        for cap in caps:
            res = self._astar(start, goal, wind_speed_10m, cap, max_expansions)
            if res is not None:
                return res
        return None

    def _astar(self, start, goal, wind10, cap, max_exp) -> PlanResult | None:
        top = self.profile.max_altitude_agl if cap is None else max(cap, self.profile.min_altitude_agl)
        kmax = int(np.searchsorted(self.levels, top + 1e-6) - 1)
        kmax = max(0, min(kmax, len(self.levels) - 1))
        s = self._snap(start, kmax)
        g = self._snap(goal, kmax)
        if s is None or g is None:
            return None
        lim = self.profile.limits.max_sustained_wind
        # Per-level multipliers: wind exposure (power law) and altitude preference.
        wind_z = np.array([wind_at_altitude(wind10, z) for z in self.levels])
        wind_pen = self.w_wind * (wind_z / lim) ** 2
        pref = self.profile.cruise_altitude_agl if cap is None else min(cap, self.profile.cruise_altitude_agl)
        alt_pen = self.w_alt * np.abs(self.levels - pref)
        # Ground risk falls with altitude a little (more time to deploy the parachute).
        pop_scale = np.clip(1.0 - self.levels / 400.0, 0.6, 1.0)

        goal_c = self._center(*g)
        nbrs = [(di, dj, dk) for di in (-1, 0, 1) for dj in (-1, 0, 1) for dk in (-1, 0, 1) if (di, dj, dk) != (0, 0, 0)]
        steps = {d: math.sqrt((d[0] * self.xy_res) ** 2 + (d[1] * self.xy_res) ** 2 + (d[2] * self.z_res) ** 2) for d in nbrs}

        free_cache: dict[tuple[int, int, int], bool] = {}

        def free(n):
            v = free_cache.get(n)
            if v is None:
                v = self._node_free(*n)
                free_cache[n] = v
            return v

        def h(n):
            return self.eps * norm(self._center(*n) - goal_c)

        open_heap = [(h(s), 0.0, s)]
        best = {s: 0.0}
        parent: dict[tuple[int, int, int], tuple[int, int, int] | None] = {s: None}
        expanded = 0
        while open_heap:
            _, gcost, n = heapq.heappop(open_heap)
            if gcost > best.get(n, math.inf) + 1e-9:
                continue
            if n == g:
                break
            expanded += 1
            if expanded > max_exp:
                return None
            i, j, k = n
            for d in nbrs:
                m = (i + d[0], j + d[1], k + d[2])
                if not (0 <= m[0] < self.nx and 0 <= m[1] < self.ny and 0 <= m[2] <= kmax):
                    continue
                if not free(m):
                    continue
                if d[0] and d[1] and not (free((i + d[0], j, k)) and free((i, j + d[1], k))):
                    continue  # no corner cutting around buildings
                km = m[2]
                mult = 1.0 + self.w_pop * self.pop_c[m[0], m[1]] * pop_scale[km] + wind_pen[km] + alt_pen[km]
                if d[2] > 0:
                    mult += 0.3  # climbing costs energy
                c = gcost + steps[d] * mult
                if c < best.get(m, math.inf):
                    best[m] = c
                    parent[m] = n
                    heapq.heappush(open_heap, (c + h(m), c, m))
        if g not in parent:
            return None
        nodes = []
        n: tuple[int, int, int] | None = g
        while n is not None:
            nodes.append(n)
            n = parent[n]
        nodes.reverse()
        pts = [self._center(*n) for n in nodes]
        pts = [np.array(start, dtype=float)] + pts[1:-1] + [np.array(goal, dtype=float)] if len(pts) > 1 else [start, goal]
        pts = self.smooth(pts)
        return PlanResult(pts, best[g], expanded, cap)

    def smooth(self, pts: list[np.ndarray]) -> list[np.ndarray]:
        """Greedy line-of-sight shortcutting against the inflated map."""
        assert self.inflated is not None
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1 and not self.inflated.segment_free(pts[i], pts[j]):
                j -= 1
            out.append(pts[j])
            i = j
        return out


class PathTracker:
    """Pure-pursuit carrot along a polyline."""

    def __init__(self, waypoints: list[np.ndarray]) -> None:
        wp = [np.asarray(w, dtype=float) for w in waypoints]
        if len(wp) == 1:
            wp = [wp[0], wp[0].copy()]
        self.wp = wp
        self.seg = 0

    @property
    def goal(self) -> np.ndarray:
        return self.wp[-1]

    @staticmethod
    def _project(pos, a, b):
        ab = b - a
        t = float(np.clip((pos - a) @ ab / max(ab @ ab, 1e-9), 0.0, 1.0))
        return t, a + t * ab

    def _advance(self, pos: np.ndarray) -> None:
        while self.seg < len(self.wp) - 2:
            t, proj = self._project(pos, self.wp[self.seg], self.wp[self.seg + 1])
            _, proj_next = self._project(pos, self.wp[self.seg + 1], self.wp[self.seg + 2])
            if t >= 0.999 or norm(pos - proj_next) < norm(pos - proj):
                self.seg += 1
            else:
                break

    def carrot(self, pos: np.ndarray, lookahead: float) -> np.ndarray:
        self._advance(pos)
        _, p = self._project(pos, self.wp[self.seg], self.wp[self.seg + 1])
        remaining = lookahead
        k = self.seg
        while True:
            end = self.wp[k + 1]
            d = norm(end - p)
            if d >= remaining or k + 1 == len(self.wp) - 1:
                return p + unit(end - p) * min(remaining, d)
            remaining -= d
            p = end
            k += 1

    def remaining_distance(self, pos: np.ndarray) -> float:
        self._advance(pos)
        d = norm(self.wp[self.seg + 1] - pos)
        for a, b in zip(self.wp[self.seg + 1 :], self.wp[self.seg + 2 :]):
            d += norm(b - a)
        return d


@dataclass
class LocalPlanResult:
    velocity: np.ndarray
    clearance: float
    blocked: bool
    candidates: int


class LocalPlanner:
    def __init__(self, inflated: InflatedMap, profile: VehicleProfile, horizon: float = 3.0, steps: int = 6,
                 latency: float = 0.3) -> None:
        self.inflated = inflated
        self.profile = profile
        self.horizon = horizon
        self.steps = steps
        self.latency = latency
        self.r_safe = profile.safety_radius_m + 1.5
        # Tight fallback used when the vehicle is already inside the inflation
        # margin (courtyards, canyons, climbing out of a drop point).
        self.tight = InflatedMap(inflated.map, profile.safety_radius_m + 0.8, 0.5)

    def command(
        self,
        pos: np.ndarray,
        vel: np.ndarray,
        carrot: np.ndarray,
        v_max: float,
        a_max: float,
        world: PerceivedWorld,
        peers: list[tuple[np.ndarray, np.ndarray]] | None = None,
        goal_dist: float | None = None,
    ) -> LocalPlanResult:
        to = carrot - pos
        d_goal = norm(to) if goal_dist is None else goal_dist
        speed_pref = min(v_max, math.sqrt(2.0 * 0.6 * a_max * max(d_goal, 0.0)))
        v_pref = unit(to) * speed_pref

        # Sample velocity space around the preferred heading.
        heading = math.atan2(v_pref[1], v_pref[0]) if speed_pref > 0.1 else math.atan2(vel[1], vel[0])
        offsets = np.radians([0, 15, -15, 30, -30, 50, -50, 75, -75, 105, -105, 140, -140, 180])
        speeds = np.array([0.25, 0.5, 0.75, 1.0]) * max(v_max, 0.5)
        climb = self.profile.max_climb_rate
        vz_opts = np.array([-0.6, -0.3, 0.0, 0.3, 0.6, 1.0]) * climb
        hs = heading + offsets
        cand = [v_pref, np.zeros(3), clamp_norm(vel.copy(), v_max)]
        base_vz = v_pref[2]
        for s in speeds:
            for a in hs:
                for vz in vz_opts:
                    cand.append(vec(s * math.cos(a), s * math.sin(a), np.clip(base_vz + vz, -self.profile.max_descent_rate, climb)))
        V = np.array(cand)
        M = len(V)

        # Roll out with an acceleration-limited transition from the current velocity.
        taus = np.linspace(self.horizon / self.steps, self.horizon, self.steps)
        dv = V - vel
        dvn = np.linalg.norm(dv, axis=1, keepdims=True)
        t_reach = dvn[:, 0] / max(a_max, 0.1)
        P = np.empty((M, self.steps, 3))
        for k, tau in enumerate(taus):
            # position: accelerate for min(tau, t_reach), then cruise
            t_acc = np.minimum(tau, t_reach)[:, None]
            p_acc = vel * t_acc + 0.5 * (dv / np.maximum(dvn, 1e-9)) * a_max * t_acc**2
            p_cru = V * np.maximum(tau - t_reach, 0.0)[:, None]
            P[:, k] = pos + p_acc + p_cru
        flat = P.reshape(-1, 3)
        geo = self.inflated if not self.inflated.blocked(pos[None, :], nofly_margin=5.0)[0] else self.tight
        hit = geo.blocked(flat, nofly_margin=5.0).reshape(M, self.steps).any(axis=1)

        clearance = np.full(M, 50.0)
        nearest = 50.0  # distance from the vehicle to the closest perceived object now
        if len(world.static_points):
            sp = world.static_points
            dist_now = np.linalg.norm(sp - pos, axis=1)
            near = sp[dist_now < self.horizon * max(v_max, 1.0) + 15.0]
            nearest = min(nearest, float(dist_now.min()))
            if len(near):
                d = np.linalg.norm(flat[:, None, :] - near[None, :, :], axis=2).min(axis=1)
                clearance = np.minimum(clearance, d.reshape(M, self.steps).min(axis=1))
        movers = [(o.position, o.velocity, o.radius) for o in world.obstacles]
        movers += [(p, v, self.profile.safety_radius_m) for p, v in (peers or [])]
        for op, ov, orad in movers:
            pred = op[None, :] + ov[None, :] * taus[:, None]  # (steps, 3)
            d = np.linalg.norm(P - pred[None, :, :], axis=2).min(axis=1) - orad
            clearance = np.minimum(clearance, d)
            if norm(ov) < 0.5:  # only static things limit braking distance; movers are handled by VO
                nearest = min(nearest, norm(op - pos) - orad)
        hit |= clearance < self.r_safe

        # Braking-distance cap: always able to stop inside observed free space.
        v_brake = math.sqrt(max(0.0, 2.0 * a_max * max(nearest - self.r_safe - self.latency * norm(vel), 0.0)))
        speeds_all = np.linalg.norm(V, axis=1)
        over = speeds_all > max(v_brake, 1.0) + 1e-6
        cost = (
            np.linalg.norm(V - v_pref, axis=1) ** 2 / max(v_max, 1.0) ** 2
            + 2.0 * np.exp(-(clearance - self.r_safe) / 3.0)
            + 0.15 * np.linalg.norm(V - vel, axis=1) / max(a_max, 0.1)
            + 3.0 * over
        )
        cost[hit] = np.inf
        best = int(np.argmin(cost))
        if not np.isfinite(cost[best]):
            # Nothing safe: brake hard (hover) and let the supervisor replan.
            return LocalPlanResult(np.zeros(3), float(clearance.max()), True, M)
        return LocalPlanResult(V[best], float(clearance[best]), False, M)
