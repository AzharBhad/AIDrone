"""Multi-modal perception with weather-aware, diverse redundancy.

Two asymmetric rules (doc 1.14, Amazon MK30 lesson):

* **Hazards are "unsafe if ANY healthy sensor says so"** - avoiding a
  phantom obstacle costs seconds, hitting a real one costs everything.
* **Irreversible affirmative actions need CONSENSUS of diverse sensors** -
  cutting motors, declaring touchdown or releasing a parcel requires
  agreement from physically different measurement principles, so light rain
  read as "ground" by one sensor can never shut the propellers down.

LiDAR rain/snow clutter is removed in two stages: a range-adaptive radius
outlier filter (raindrops are isolated points, structures return clusters)
and temporal persistence (real structure returns in consecutive scans at the
same world voxel, raindrops do not).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .citymap import CityMap
from .geometry import norm
from .measurements import CameraDetection, RadarTrack, RangefinderMeasurement, SensorFrame


@dataclass
class TrackedObstacle:
    position: np.ndarray
    velocity: np.ndarray
    radius: float
    sources: set[str]
    confidence: float
    label: str = "unknown"

    @property
    def dynamic(self) -> bool:
        return norm(self.velocity) > 0.5


@dataclass
class PerceivedWorld:
    static_points: np.ndarray  # confirmed unmapped obstacle points (N, 3)
    obstacles: list[TrackedObstacle] = field(default_factory=list)
    people: list[CameraDetection] = field(default_factory=list)
    clutter_rejected: int = 0


class PerceptionFusion:
    def __init__(self, hd_map: CityMap, voxel: float = 1.5, persistence: int = 2, window: int = 4) -> None:
        self.map = hd_map
        self.voxel = voxel
        self.persistence = persistence
        self.history: deque[set[tuple[int, int, int]]] = deque(maxlen=window)
        self.confirmed: dict[tuple[int, int, int], float] = {}  # voxel -> last seen t
        self.memory_s = 20.0
        self.beam_spacing_rad = np.radians(7.5)

    def _explained_by_map(self, pts: np.ndarray, origin: np.ndarray) -> np.ndarray:
        dirs = pts - origin
        d = np.linalg.norm(dirs, axis=1, keepdims=True)
        dirs = dirs / np.maximum(d, 1e-6)
        deeper = pts + dirs * 0.8
        return self.map.occupied(deeper, include_unmapped=False) | self.map.occupied(pts, include_unmapped=False)

    def _isolated(self, pts: np.ndarray, origin: np.ndarray) -> np.ndarray:
        """Dynamic Radius Outlier Removal: raindrops/snowflakes have no neighbours.

        The search radius grows with range to match the beam spacing, so sparse
        returns from a distant real structure are kept.
        """
        n = len(pts)
        if n == 0:
            return np.zeros(0, dtype=bool)
        rng = np.linalg.norm(pts - origin, axis=1)
        radius = np.maximum(0.6, 1.5 * self.beam_spacing_rad * rng)
        d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
        np.fill_diagonal(d, np.inf)
        return (d < radius[:, None]).sum(axis=1) < 1

    def update(self, frame: SensorFrame, drone_pos: np.ndarray, weights: dict[str, float]) -> PerceivedWorld:
        t = frame.t
        rejected = 0
        # Sensor-relative -> world using the navigation estimate.
        radar = [RadarTrack(r.position + drone_pos, r.velocity, r.radius) for r in frame.radar]
        camera = [CameraDetection(c.position + drone_pos, c.label, c.confidence) for c in frame.camera]
        radar_pts = np.array([r.position for r in radar]) if radar else np.zeros((0, 3))

        if frame.lidar_points is not None and len(frame.lidar_points) and weights.get("lidar", 1.0) > 0.05:
            pts = frame.lidar_points + drone_pos
            pts = pts[~self._explained_by_map(pts, drone_pos)]
            isolated = self._isolated(pts, drone_pos)
            rejected += int(isolated.sum())
            pts = pts[~isolated]
            keys = {tuple(k) for k in np.floor(pts / self.voxel).astype(int)}
            self.history.append(keys)
            counts: dict[tuple[int, int, int], int] = {}
            for scan in self.history:
                for k in scan:
                    counts[k] = counts.get(k, 0) + 1
            for k in keys:
                center = (np.array(k) + 0.5) * self.voxel
                radar_ok = len(radar_pts) and np.min(np.linalg.norm(radar_pts - center, axis=1)) < 2.5
                if counts.get(k, 0) >= self.persistence or radar_ok:
                    self.confirmed[k] = t
                else:
                    rejected += 1
        # Forget stale voxels (obstacle left the sensor field of view long ago).
        for k in [k for k, ts in self.confirmed.items() if t - ts > self.memory_s]:
            del self.confirmed[k]
        static = (np.array(list(self.confirmed.keys())) + 0.5) * self.voxel if self.confirmed else np.zeros((0, 3))

        obstacles: list[TrackedObstacle] = []
        for r in radar:
            obstacles.append(self._fuse_radar(r, camera, static))
        for c in camera:
            if c.label in ("bird", "drone") and c.confidence * weights.get("camera", 1.0) > 0.35:
                if not any(norm(o.position - c.position) < 3.0 for o in obstacles):
                    obstacles.append(TrackedObstacle(c.position, np.zeros(3), 1.0, {"camera"}, c.confidence, c.label))
        people = [c for c in camera if c.label == "person" and c.confidence > 0.3]
        return PerceivedWorld(static, obstacles, people, rejected)

    @staticmethod
    def _fuse_radar(r: RadarTrack, cams: list[CameraDetection], static: np.ndarray) -> TrackedObstacle:
        sources = {"radar"}
        conf = 0.7
        label = "unknown"
        for c in cams:
            if norm(c.position - r.position) < 3.0:
                sources.add("camera")
                conf = max(conf, 0.7 + 0.3 * c.confidence)
                label = c.label
        if len(static) and np.min(np.linalg.norm(static - r.position, axis=1)) < 2.5:
            sources.add("lidar")
            conf = min(1.0, conf + 0.15)
        return TrackedObstacle(r.position, r.velocity, r.radius, sources, conf, label)


def landing_zone_clear(
    world: PerceivedWorld,
    center: np.ndarray,
    radius: float,
    ground_z: float,
) -> tuple[bool, list[str]]:
    """Any modality reporting a person/obstacle on the pad blocks the descent."""
    reasons = []
    for p in world.people:
        if np.hypot(*(p.position[:2] - center[:2])) < radius:
            reasons.append(f"person on/near pad (camera {p.confidence:.2f})")
    for o in world.obstacles:
        if np.hypot(*(o.position[:2] - center[:2])) < radius and o.position[2] < ground_z + 4.0:
            reasons.append(f"{o.label} object near pad ({'+'.join(sorted(o.sources))})")
    if len(world.static_points):
        d = np.hypot(world.static_points[:, 0] - center[0], world.static_points[:, 1] - center[1])
        near = (d < radius) & (world.static_points[:, 2] > ground_z + 0.3) & (world.static_points[:, 2] < ground_z + 4.0)
        if near.any():
            reasons.append(f"unmapped object on pad (LiDAR, {int(near.sum())} voxels)")
    return (not reasons), reasons


@dataclass
class TouchdownVotes:
    rangefinder: bool
    nav_height: bool
    vertical_rest: bool
    thrust_unloaded: bool

    @property
    def count(self) -> int:
        return sum((self.rangefinder, self.nav_height, self.vertical_rest, self.thrust_unloaded))

    @property
    def consensus(self) -> bool:
        # >= 3 of 4 AND both an exteroceptive and a proprioceptive principle agree.
        extero = self.rangefinder or self.nav_height
        proprio = self.vertical_rest and self.thrust_unloaded
        return self.count >= 3 and extero and proprio


def touchdown_votes(
    rangefinder: RangefinderMeasurement | None,
    height_above_pad: float,
    vertical_speed: float,
    thrust_ratio: float,
) -> TouchdownVotes:
    return TouchdownVotes(
        rangefinder=bool(rangefinder is not None and rangefinder.valid and rangefinder.distance < 0.35),
        nav_height=height_above_pad < 0.4,
        vertical_rest=abs(vertical_speed) < 0.25,
        thrust_unloaded=thrust_ratio < 0.6,
    )
