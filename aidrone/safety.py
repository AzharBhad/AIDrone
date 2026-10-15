"""Energy management and the safety supervisor (docs 1.14 + safety_protocols.yaml).

Failsafe levels follow ``AIDroneOperations/Part2.../3.safety_protocols.yaml``
(level 1: slow/optimise, level 2: return to base, level 3: emergency
landing) with two upgrades:

* battery thresholds are *energy-margin* based (energy to get home against
  the actual measured wind, plus reserve) instead of fixed percentages - a
  30 % battery is plenty with a tailwind and fatal into a 14 m/s headwind;
* emergency landings go to the best pre-surveyed contingency site (park,
  depot) reachable with the remaining energy, minimising ground risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum

import numpy as np

from .citymap import CityMap
from .config import VehicleProfile
from .geometry import AIR_DENSITY, GRAVITY, horizontal, norm, unit
from .localization import NavSolution
from .weather import OpsLevel, WeatherAssessment

AVIONICS_POWER_W = 70.0


class EnergyModel:
    def __init__(self, profile: VehicleProfile) -> None:
        self.p = profile

    def power(self, mass: float, airspeed: float, gust_std: float = 0.0, heaters: bool = False) -> float:
        hover = self.p.hover_power_w_per_kg * mass
        induced = hover * (1.0 - 0.2 * min(airspeed / 12.0, 1.0))  # translational lift
        parasitic = 0.5 * AIR_DENSITY * self.p.drag_area_m2 * airspeed**3
        turbulence = hover * 0.04 * gust_std  # constant attitude corrections
        heater = self.p.heater_power_w if heaters else 0.0
        return induced + parasitic + turbulence + heater + AVIONICS_POWER_W

    def usable_wh(self, temperature_c: float) -> float:
        derate = 0.008 * max(0.0, 10.0 - temperature_c)
        if self.p.battery_heater:
            derate *= 0.4
        return self.p.battery_wh * max(0.5, 1.0 - derate)

    def leg(
        self,
        a: np.ndarray,
        b: np.ndarray,
        mass: float,
        wind: np.ndarray,
        ground_speed: float,
        gust_std: float = 0.0,
        heaters: bool = False,
    ) -> tuple[float, float]:
        """(energy Wh, time s) for a straight leg; inf if the wind cannot be beaten."""
        d = b - a
        dh = norm(horizontal(d))
        dz = d[2]
        t_h, e_h = 0.0, 0.0
        if dh > 1e-6:
            u = unit(horizontal(d))
            vg = ground_speed
            for _ in range(8):  # lower ground speed until required airspeed is achievable
                va = norm(u * vg - horizontal(wind))
                if va <= self.p.max_airspeed:
                    break
                vg *= 0.8
            if norm(u * vg - horizontal(wind)) > self.p.max_airspeed or vg < 1.0:
                return math.inf, math.inf
            t_h = dh / vg
            e_h = self.power(mass, va, gust_std, heaters) * t_h / 3600.0
        t_v = abs(dz) / (self.p.max_climb_rate * 0.7 if dz > 0 else self.p.max_descent_rate * 0.7)
        e_v = self.power(mass, 0.0, gust_std, heaters) * t_v / 3600.0
        if dz > 0:
            e_v += mass * GRAVITY * dz / 0.6 / 3600.0  # potential energy at ~60 % efficiency
        return e_h + e_v, t_h + t_v

    def path(self, pts: list[np.ndarray], mass: float, wind_at, ground_speed: float, gust_std: float = 0.0,
             heaters: bool = False) -> tuple[float, float]:
        e = t = 0.0
        for a, b in zip(pts, pts[1:]):
            de, dt = self.leg(a, b, mass, wind_at(0.5 * (a + b)), ground_speed, gust_std, heaters)
            e += de
            t += dt
        return e, t


@dataclass
class Battery:
    capacity_wh: float
    energy_wh: float

    @property
    def soc(self) -> float:
        return self.energy_wh / self.capacity_wh

    def draw(self, power_w: float, dt: float) -> None:
        self.energy_wh = max(0.0, self.energy_wh - power_w * dt / 3600.0)


class FailsafeLevel(IntEnum):
    NONE = 0
    LEVEL_1 = 1  # route optimisation / speed reduction / alert
    LEVEL_2 = 2  # return to base, backup systems
    LEVEL_3 = 3  # emergency landing / parachute


class SafetyAction(IntEnum):
    CONTINUE = 0
    SLOW = 1
    HOLD = 2
    RETURN = 3
    DIVERT_LAND = 4
    EMERGENCY_LAND = 5
    PARACHUTE = 6


@dataclass
class SafetyDecision:
    level: FailsafeLevel
    action: SafetyAction
    speed_factor: float = 1.0
    reasons: list[str] = field(default_factory=list)
    landing_site: np.ndarray | None = None


@dataclass
class VehicleHealth:
    motors_ok: int
    motor_count: int
    thrust_to_weight: float
    link_age_s: float
    descent_rate: float  # positive = falling
    commanded_descent: float


class Geofence:
    def __init__(self, city: CityMap, margin: float = 10.0) -> None:
        self.city = city
        self.margin = margin

    def violation(self, pos: np.ndarray, vel: np.ndarray, lookahead_s: float = 1.5) -> str | None:
        for p in (pos, pos + vel * lookahead_s):  # current and predicted position
            if not self.city.in_bounds(p, self.margin):
                return "operational boundary"
            z = self.city.in_no_fly(p)
            if z is not None:
                return f"no-fly zone '{z.name}'"
        return None


def choose_landing_site(
    city: CityMap,
    pos: np.ndarray,
    energy_left_wh: float,
    energy: EnergyModel,
    mass: float,
    wind: np.ndarray,
) -> np.ndarray | None:
    """Lowest-risk reachable pre-surveyed site (risk = population * distance)."""
    best, best_cost = None, math.inf
    for s in city.safe_landing_sites:
        target = np.array([s[0], s[1], pos[2]])
        e, _ = energy.leg(pos, target, mass, wind, 8.0)
        e += energy.leg(target, s, mass, wind, 0.0)[0]
        if e > 0.9 * energy_left_wh:
            continue
        cost = norm(s - pos) * (0.2 + city.population_at(s[0], s[1]))
        if cost < best_cost:
            best, best_cost = s.copy(), cost
    return best


class SafetySupervisor:
    def __init__(self, city: CityMap, profile: VehicleProfile) -> None:
        self.city = city
        self.profile = profile
        self.energy = EnergyModel(profile)
        self.geofence = Geofence(city)
        self.nav_bad_since: float | None = None

    def evaluate(
        self,
        t: float,
        pos: np.ndarray,
        vel: np.ndarray,
        nav: NavSolution,
        weather: WeatherAssessment,
        battery: Battery,
        usable_wh: float,
        energy_home_wh: float,
        health: VehicleHealth,
        mass: float,
        wind: np.ndarray,
        returning: bool = False,
    ) -> SafetyDecision:
        reasons: list[str] = []
        level = FailsafeLevel.NONE
        action = SafetyAction.CONTINUE
        speed = 1.0
        site = None

        def escalate(lv: FailsafeLevel, act: SafetyAction, why: str) -> None:
            nonlocal level, action
            reasons.append(why)
            if act > action:
                action = act
            if lv > level:
                level = lv

        # --- airframe: catastrophic first
        agl = pos[2] - self.city.ground_level(pos[0], pos[1])
        uncommanded_fall = health.descent_rate - max(health.commanded_descent, 0.0)
        if health.thrust_to_weight < 1.02 or uncommanded_fall > 6.0:
            if self.profile.has_parachute and agl > 15.0:
                escalate(FailsafeLevel.LEVEL_3, SafetyAction.PARACHUTE, "loss of lift: deploy parachute")
            else:
                escalate(FailsafeLevel.LEVEL_3, SafetyAction.EMERGENCY_LAND, "loss of lift: controlled descent")
        elif health.motors_ok < health.motor_count:
            escalate(FailsafeLevel.LEVEL_2, SafetyAction.DIVERT_LAND,
                     f"motor failure ({health.motors_ok}/{health.motor_count} healthy, T/W {health.thrust_to_weight:.2f})")

        # --- weather
        if weather.level == OpsLevel.LAND_NOW:
            escalate(FailsafeLevel.LEVEL_3, SafetyAction.DIVERT_LAND, "weather: " + "; ".join(weather.reasons))
        elif weather.level >= OpsLevel.HOLD:
            escalate(FailsafeLevel.LEVEL_2, SafetyAction.RETURN, "weather: " + "; ".join(weather.reasons))
        elif weather.level >= OpsLevel.CAUTION:
            speed = min(speed, weather.speed_factor)
            if weather.level == OpsLevel.DEGRADED:
                escalate(FailsafeLevel.LEVEL_1, SafetyAction.SLOW, "weather: " + "; ".join(weather.reasons))

        # --- energy (margin based)
        reserve = self.profile.reserve_fraction * usable_wh
        remaining_usable = battery.energy_wh - (self.profile.battery_wh - usable_wh)
        margin = remaining_usable - energy_home_wh - reserve
        if remaining_usable < 0.5 * reserve:
            escalate(FailsafeLevel.LEVEL_3, SafetyAction.EMERGENCY_LAND, "battery critical")
        elif margin < 0 and not math.isinf(energy_home_wh):
            escalate(FailsafeLevel.LEVEL_2, SafetyAction.DIVERT_LAND if margin < -reserve * 0.5 else SafetyAction.RETURN,
                     f"energy margin {margin:.0f} Wh: cannot complete and keep reserve")
        elif math.isinf(energy_home_wh):
            escalate(FailsafeLevel.LEVEL_2, SafetyAction.DIVERT_LAND, "cannot make headway home against wind")
        elif margin < 0.25 * reserve:
            escalate(FailsafeLevel.LEVEL_1, SafetyAction.SLOW, "energy margin low: economy speed")
            speed = min(speed, 0.8)

        # --- navigation integrity
        if not nav.integrity_ok:
            self.nav_bad_since = t if self.nav_bad_since is None else self.nav_bad_since
            dur = t - self.nav_bad_since
            speed = min(speed, 0.5)
            if dur > 20.0:
                escalate(FailsafeLevel.LEVEL_2, SafetyAction.RETURN, f"nav integrity lost {dur:.0f}s (HPL {nav.hpl:.1f} m)")
            else:
                escalate(FailsafeLevel.LEVEL_1, SafetyAction.SLOW, f"nav integrity degraded (HPL {nav.hpl:.1f} m > {nav.alert_limit} m)")
        else:
            self.nav_bad_since = None

        # --- C2 link: the aircraft is autonomous, link loss means follow the pre-planned contingency
        if health.link_age_s > 120.0:
            escalate(FailsafeLevel.LEVEL_2, SafetyAction.RETURN, "C2 link lost > 120 s")
        elif health.link_age_s > 5.0:
            escalate(FailsafeLevel.LEVEL_1, SafetyAction.CONTINUE, "C2 link degraded: continuing autonomously")

        # --- geofence
        gf = self.geofence.violation(pos, vel)
        if gf:
            escalate(FailsafeLevel.LEVEL_1, SafetyAction.HOLD, f"geofence: approaching {gf}")

        if returning and action == SafetyAction.RETURN:
            action = SafetyAction.CONTINUE  # already doing it
        if action in (SafetyAction.DIVERT_LAND, SafetyAction.EMERGENCY_LAND):
            site = choose_landing_site(self.city, pos, max(remaining_usable, 0.0), self.energy, mass, wind)
            if site is None:
                action = SafetyAction.EMERGENCY_LAND
        return SafetyDecision(level, action, speed, reasons, site)
