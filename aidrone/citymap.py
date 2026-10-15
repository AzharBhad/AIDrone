"""Urban environment model: 2.5D building height map plus 3D extras.

The same class serves two roles:

* ground truth for the simulator (``include_unmapped=True``): ray casting
  for LiDAR/GNSS visibility and collision checks;
* the onboard HD map (``include_unmapped=False``): what the planner knows
  before take-off. Unmapped obstacles (cranes, scaffolding) must be found by
  perception in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import vec


@dataclass
class NoFlyZone:
    name: str
    center: tuple[float, float]
    radius: float
    z_min: float = -1.0
    z_max: float = 1e9

    def contains(self, p: np.ndarray, margin: float = 0.0) -> bool:
        d = np.hypot(p[0] - self.center[0], p[1] - self.center[1])
        return bool(d < self.radius + margin and self.z_min - margin <= p[2] <= self.z_max + margin)


@dataclass
class BoxObstacle:
    name: str
    lo: np.ndarray
    hi: np.ndarray
    mapped: bool = True

    def contains(self, pts: np.ndarray) -> np.ndarray:
        return np.all((pts >= self.lo) & (pts <= self.hi), axis=-1)


@dataclass
class DeliverySite:
    """A customer drop point.

    kind:
      * ``pad``    - open area, the drone lands, releases, and climbs out;
      * ``winch``  - too tight or too crowded to land (courtyard, balcony,
                     front garden): the drone hovers high and lowers the
                     parcel on a tether.
    """

    site_id: str
    position: np.ndarray
    kind: str = "pad"
    anchors: list[np.ndarray] = field(default_factory=list)  # UWB anchors (>= 4, non-coplanar)
    tag_size_m: float = 0.6
    winch_hover_agl: float = 18.0

    @property
    def ground_z(self) -> float:
        return float(self.position[2])


def _uwb_anchors(center: np.ndarray, spread: float = 2.5) -> list[np.ndarray]:
    # Four non-coplanar anchors around the pad (doc 1.8: >= 4, non-coplanar).
    c = center
    return [
        c + vec(spread, spread, 0.3),
        c + vec(-spread, spread, 1.8),
        c + vec(-spread, -spread, 0.3),
        c + vec(spread, -spread, 1.2),
    ]


class CityMap:
    def __init__(
        self,
        heights: np.ndarray,
        resolution: float,
        population: np.ndarray,
        no_fly: list[NoFlyZone] | None = None,
        obstacles: list[BoxObstacle] | None = None,
        sites: dict[str, DeliverySite] | None = None,
        depot: DeliverySite | None = None,
        safe_landing_sites: list[np.ndarray] | None = None,
    ) -> None:
        self.heights = heights.astype(float)
        self.res = float(resolution)
        self.population = population.astype(float)
        self.no_fly = list(no_fly or [])
        self.obstacles = list(obstacles or [])
        self.sites = dict(sites or {})
        self.depot = depot
        self.safe_landing_sites = list(safe_landing_sites or [])
        self.nx, self.ny = heights.shape
        self.size_x = self.nx * self.res
        self.size_y = self.ny * self.res

    # ------------------------------------------------------------------ lookup
    def _idx(self, x, y):
        ix = np.floor(np.asarray(x) / self.res).astype(int)
        iy = np.floor(np.asarray(y) / self.res).astype(int)
        inside = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        return np.clip(ix, 0, self.nx - 1), np.clip(iy, 0, self.ny - 1), inside

    def height_at(self, x, y):
        ix, iy, inside = self._idx(x, y)
        h = np.where(inside, self.heights[ix, iy], 0.0)
        return float(h) if np.ndim(h) == 0 else h

    def population_at(self, x, y):
        ix, iy, inside = self._idx(x, y)
        p = np.where(inside, self.population[ix, iy], 0.0)
        return float(p) if np.ndim(p) == 0 else p

    def in_bounds(self, p: np.ndarray, margin: float = 0.0) -> bool:
        return bool(margin <= p[0] <= self.size_x - margin and margin <= p[1] <= self.size_y - margin)

    def ground_level(self, x: float, y: float) -> float:
        """Surface height under a point: rooftop or street (0)."""
        return float(self.height_at(x, y))

    def mean_height_around(self, x: float, y: float, radius: float = 30.0) -> float:
        r = max(1, int(radius / self.res))
        ix, iy, _ = self._idx(x, y)
        window = self.heights[max(0, ix - r) : ix + r + 1, max(0, iy - r) : iy + r + 1]
        return float(window.mean()) if window.size else 0.0

    def street_axis(self, x: float, y: float, z: float, probe: float = 25.0) -> np.ndarray | None:
        """Direction of the street canyon at (x, y) below rooftop level, if any."""
        free = []
        for d in (vec(1, 0, 0), vec(0, 1, 0)):
            s = np.linspace(-probe, probe, 11)
            hs = self.height_at(x + d[0] * s, y + d[1] * s)
            free.append(float(np.mean(hs < z)))
        if max(free) < 0.6 or abs(free[0] - free[1]) < 0.15:
            return None
        return vec(1, 0, 0) if free[0] > free[1] else vec(0, 1, 0)

    # --------------------------------------------------------------- occupancy
    def occupied(
        self,
        pts: np.ndarray,
        include_unmapped: bool = True,
        dynamic: list[tuple[np.ndarray, float]] | None = None,
    ) -> np.ndarray:
        pts = np.asarray(pts, dtype=float)
        flat = pts.reshape(-1, 3)
        h = self.height_at(flat[:, 0], flat[:, 1])
        occ = (flat[:, 2] < np.asarray(h)) | (flat[:, 2] < 0.0)
        for ob in self.obstacles:
            if ob.mapped or include_unmapped:
                occ |= ob.contains(flat)
        for center, radius in dynamic or []:
            occ |= np.sum((flat - center) ** 2, axis=1) < radius * radius
        return occ.reshape(pts.shape[:-1])

    def raycast(
        self,
        origin: np.ndarray,
        dirs: np.ndarray,
        max_range: float,
        step: float = 1.0,
        include_unmapped: bool = True,
        dynamic: list[tuple[np.ndarray, float]] | None = None,
    ) -> np.ndarray:
        """Range to first hit along each unit direction (inf if none)."""
        steps = np.arange(step, max_range + 1e-9, step)
        if len(steps) == 0 or len(dirs) == 0:
            return np.full(len(dirs), np.inf)
        pts = origin[None, None, :] + dirs[:, None, :] * steps[None, :, None]
        occ = self.occupied(pts, include_unmapped=include_unmapped, dynamic=dynamic)
        hit = occ.any(axis=1)
        first = occ.argmax(axis=1)
        return np.where(hit, steps[first], np.inf)

    def line_of_sight(self, a: np.ndarray, b: np.ndarray, step: float = 1.0, include_unmapped: bool = True) -> bool:
        d = b - a
        n = np.linalg.norm(d)
        if n < 1e-6:
            return True
        k = max(2, int(n / step))
        pts = a[None, :] + d[None, :] * np.linspace(0.0, 1.0, k)[:, None]
        return not bool(self.occupied(pts, include_unmapped=include_unmapped).any())

    def in_no_fly(self, p: np.ndarray, margin: float = 0.0) -> NoFlyZone | None:
        for z in self.no_fly:
            if z.contains(p, margin):
                return z
        return None

    # --------------------------------------------------------------- generator
    @classmethod
    def generate(
        cls,
        size: float = 640.0,
        resolution: float = 2.0,
        block: float = 60.0,
        street: float = 20.0,
        seed: int = 7,
    ) -> CityMap:
        """Procedural Manhattan-style downtown with parks, plazas and towers."""
        rng = np.random.default_rng(seed)
        n = int(size / resolution)
        heights = np.zeros((n, n))
        population = np.ones((n, n))  # streets are busiest
        period = block + street
        nb = int(size // period)
        kinds: dict[tuple[int, int], str] = {}
        for bi in range(nb):
            for bj in range(nb):
                r = rng.random()
                kinds[(bi, bj)] = "buildings" if r < 0.72 else ("park" if r < 0.87 else "plaza")
        # Fixed features so scenarios are reproducible across seeds.
        kinds[(0, 0)] = "depot"
        kinds[(nb - 2, nb - 2)] = "park"
        kinds[(1, nb - 2)] = "courtyard"
        kinds[(nb - 2, 1)] = "buildings"
        kinds[(nb // 2, nb // 2)] = "buildings"

        def cells(x0, y0, w, h):
            return (
                slice(int(x0 / resolution), int((x0 + w) / resolution)),
                slice(int(y0 / resolution), int((y0 + h) / resolution)),
            )

        for (bi, bj), kind in kinds.items():
            x0 = street / 2 + bi * period
            y0 = street / 2 + bj * period
            sx, sy = cells(x0, y0, block, block)
            if kind == "buildings":
                lot = block / 2
                for li in range(2):
                    for lj in range(2):
                        h = float(np.clip(rng.lognormal(np.log(28.0), 0.55), 9.0, 95.0))
                        if rng.random() < 0.08:
                            h = float(rng.uniform(100.0, 140.0))  # towers poke above cruise altitude
                        lx, ly = cells(x0 + li * lot + 1.5, y0 + lj * lot + 1.5, lot - 3, lot - 3)
                        heights[lx, ly] = h
                population[sx, sy] = 0.15  # people inside buildings are sheltered
            elif kind == "courtyard":
                # Ring of mid-rise buildings around a 16 m inner courtyard.
                heights[sx, sy] = 32.0
                cx, cy = cells(x0 + block / 2 - 8, y0 + block / 2 - 8, 16, 16)
                heights[cx, cy] = 0.0
                population[sx, sy] = 0.15
                population[cx, cy] = 0.6
            elif kind == "park":
                population[sx, sy] = 0.45
            elif kind == "plaza":
                population[sx, sy] = 0.9
            elif kind == "depot":
                population[sx, sy] = 0.05  # fenced operations yard

        def block_center(bi, bj):
            return street / 2 + bi * period + block / 2, street / 2 + bj * period + block / 2

        dx, dy = block_center(0, 0)
        depot = DeliverySite("depot", vec(dx, dy, 0.0), "pad", _uwb_anchors(vec(dx, dy, 0.0)))

        sites: dict[str, DeliverySite] = {}
        px, py = block_center(nb - 2, nb - 2)
        sites["park_pad"] = DeliverySite("park_pad", vec(px, py, 0.0), "pad", _uwb_anchors(vec(px, py, 0.0)))

        cx, cy = block_center(1, nb - 2)
        sites["courtyard_winch"] = DeliverySite(
            "courtyard_winch", vec(cx, cy, 0.0), "winch", _uwb_anchors(vec(cx, cy, 0.0), spread=3.0)
        )

        # Rooftop helipad on a mid-rise building.
        rx, ry = block_center(nb - 2, 1)
        lot = block / 2
        sx, sy = cells(rx - lot + 1.5, ry - lot + 1.5, lot - 3, lot - 3)
        heights[sx, sy] = 40.0
        roof = vec(rx - lot / 2, ry - lot / 2, 40.0)
        sites["rooftop_pad"] = DeliverySite("rooftop_pad", roof, "pad", _uwb_anchors(roof))

        # A hospital helipad in the middle of town is a permanent no-fly zone.
        hx, hy = block_center(nb // 2, nb // 2)
        no_fly = [NoFlyZone("hospital_heliport", (hx, hy), radius=55.0)]

        # Pre-surveyed contingency landing sites: parks and the depot.
        safe = [depot.position.copy()]
        for (bi, bj), kind in kinds.items():
            if kind == "park":
                bx, by = block_center(bi, bj)
                safe.append(vec(bx, by, 0.0))

        return cls(
            heights,
            resolution,
            population,
            no_fly=no_fly,
            obstacles=[],
            sites=sites,
            depot=depot,
            safe_landing_sites=safe,
        )
