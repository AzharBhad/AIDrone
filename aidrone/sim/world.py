"""Ground-truth world for simulation: city, weather, traffic, people, faults."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..citymap import CityMap
from ..weather import WeatherTimeline, WindField


@dataclass
class MovingObject:
    name: str
    start: np.ndarray
    velocity: np.ndarray
    radius: float = 1.0
    label: str = "drone"
    t_start: float = 0.0
    t_end: float = math.inf

    def position(self, t: float) -> np.ndarray | None:
        if not self.t_start <= t <= self.t_end:
            return None
        return self.start + self.velocity * (t - self.t_start)


@dataclass
class Person:
    position: np.ndarray
    t_leave: float = math.inf  # walks away at this time

    def present(self, t: float) -> bool:
        return t < self.t_leave


@dataclass
class World:
    city: CityMap  # ground truth (includes unmapped obstacles)
    timeline: WeatherTimeline
    seed: int = 0
    movers: list[MovingObject] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    link_outages: list[tuple[float, float]] = field(default_factory=list)
    motor_failures: list[float] = field(default_factory=list)
    marker_payloads: dict[str, bytes] = field(default_factory=dict)  # site_id -> printed marker
    marker_positions: dict[str, np.ndarray] = field(default_factory=dict)  # where the marker physically is
    rtk_available: bool = True

    def __post_init__(self) -> None:
        self.wind = WindField(self.timeline, self.city, seed=self.seed)

    def weather(self, t: float):
        return self.timeline.at(t)

    def dynamic_spheres(self, t: float) -> list[tuple[np.ndarray, float]]:
        out = []
        for m in self.movers:
            p = m.position(t)
            if p is not None:
                out.append((p, m.radius))
        return out

    def link_ok(self, t: float) -> bool:
        return not any(a <= t <= b for a, b in self.link_outages)

    def marker_position(self, site_id: str) -> np.ndarray:
        if site_id in self.marker_positions:
            return self.marker_positions[site_id]
        site = self.city.sites.get(site_id) or self.city.depot
        return site.position
