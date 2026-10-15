"""Weather: truth model, onboard wind estimation and the go/slow/abort brain.

Implements ``Documentation/Process/1.11 Adverse Weather Operations.md``:

* real-time wind estimation from IMU/GNSS/thrust (no pitot needed),
* sensor de-rating per condition (rain -> LiDAR, fog -> camera/VIO, ...),
* a clearly defined envelope with graded responses instead of a binary
  fly/no-fly flag.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from enum import IntEnum

import numpy as np

from .config import VehicleProfile
from .geometry import G_VEC, norm, vec


WIND_SHEAR_ALPHA = 0.25  # urban power-law exponent: wind grows ~1.8x from 10 m to 100 m


def wind_at_altitude(wind_10m: float, agl: float) -> float:
    return wind_10m * (max(agl, 2.0) / 10.0) ** WIND_SHEAR_ALPHA


@dataclass(frozen=True)
class WeatherState:
    wind_speed: float = 0.0  # mean m/s at 10 m AGL
    wind_from_deg: float = 270.0  # meteorological: direction the wind blows FROM
    gust_std: float = 0.0  # turbulence intensity (1 sigma, m/s)
    rain_mm_h: float = 0.0
    snow_mm_h: float = 0.0
    freezing_rain: bool = False
    visibility_m: float = 10_000.0
    temperature_c: float = 15.0
    humidity: float = 0.5
    lightning_km: float = math.inf  # distance to nearest recent strike
    illumination_lux: float = 20_000.0

    def wind_vector(self) -> np.ndarray:
        """ENU vector of air motion at 10 m (blows *towards*)."""
        a = math.radians(self.wind_from_deg)
        return -self.wind_speed * vec(math.sin(a), math.cos(a), 0.0)

    @property
    def icing_risk(self) -> bool:
        moist = self.rain_mm_h > 0 or self.snow_mm_h > 0 or self.visibility_m < 1000 or self.humidity > 0.95
        return self.freezing_rain or (-15.0 <= self.temperature_c <= 2.0 and moist)

    @property
    def peak_gust(self) -> float:
        return self.wind_speed + 3.0 * self.gust_std


class WeatherTimeline:
    """Piecewise-linear weather over mission time (e.g. a squall line arriving)."""

    def __init__(self, points: list[tuple[float, WeatherState]] | WeatherState) -> None:
        if isinstance(points, WeatherState):
            points = [(0.0, points)]
        self.points = sorted(points, key=lambda p: p[0])

    def at(self, t: float) -> WeatherState:
        pts = self.points
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for (t0, a), (t1, b) in zip(pts, pts[1:]):
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                vals = {}
                for fd in fields(WeatherState):
                    va, vb = getattr(a, fd.name), getattr(b, fd.name)
                    if isinstance(va, bool):
                        vals[fd.name] = va if f < 0.5 else vb
                    elif math.isinf(va) or math.isinf(vb):
                        # "No strikes" is treated as 200 km so a storm can approach smoothly.
                        fa, fb = min(va, 200.0), min(vb, 200.0)
                        v = fa + f * (fb - fa)
                        vals[fd.name] = math.inf if v >= 200.0 else v
                    elif fd.name == "wind_from_deg":
                        d = ((vb - va + 180.0) % 360.0) - 180.0
                        vals[fd.name] = (va + f * d) % 360.0
                    else:
                        vals[fd.name] = va + f * (vb - va)
                return WeatherState(**vals)
        return pts[-1][1]

    def worst_between(self, t0: float, t1: float, step: float = 30.0) -> list[WeatherState]:
        ts = np.arange(t0, t1 + step, step)
        return [self.at(float(t)) for t in ts]


class WindField:
    """Truth wind: power-law profile, street-canyon channelling, OU turbulence."""

    def __init__(self, timeline: WeatherTimeline, city=None, seed: int = 0, alpha: float = WIND_SHEAR_ALPHA,
                 gust_length_m: float = 60.0) -> None:
        self.timeline = timeline
        self.city = city
        self.alpha = alpha
        self.gust_length = gust_length_m
        self.rng = np.random.default_rng(seed)
        self.gust_state = np.zeros(3)

    def step(self, t: float, dt: float) -> None:
        w = self.timeline.at(t)
        tau = self.gust_length / max(2.0, w.wind_speed)
        a = math.exp(-dt / tau)
        self.gust_state = a * self.gust_state + math.sqrt(1 - a * a) * self.rng.standard_normal(3)

    def wind_at(self, pos: np.ndarray, t: float) -> np.ndarray:
        w = self.timeline.at(t)
        ground = self.city.ground_level(pos[0], pos[1]) if self.city is not None else 0.0
        z = max(pos[2] - min(ground, pos[2]), 2.0)
        mean = w.wind_vector() * (z / 10.0) ** self.alpha
        turb = self.gust_state * w.gust_std * vec(1.0, 1.0, 0.5)
        if self.city is not None:
            canopy = self.city.mean_height_around(pos[0], pos[1], 30.0)
            if pos[2] < canopy:
                axis = self.city.street_axis(pos[0], pos[1], pos[2])
                if axis is not None:
                    along = float(mean @ axis) * axis
                    mean = 1.25 * along + 0.25 * (mean - along)  # channelling (Venturi) along the street
                else:
                    mean = 0.5 * mean  # sheltered pocket
                turb = turb * 1.8  # building wakes are rough
        return mean + turb


class WindEstimator:
    """Onboard wind/turbulence estimator (no pitot tube needed).

    Aerodynamic force is the residual between measured acceleration and the
    known thrust + gravity: ``F = m*a - m*a_thrust - m*g``. Inverting the
    drag law ``F = -(k + c|v_air|) v_air`` (linear rotor drag + quadratic body
    drag) gives the air-relative velocity; wind = ground velocity - air
    velocity. A random-walk Kalman filter smooths the mean wind and the
    innovation statistics give the turbulence (gust) intensity.
    """

    def __init__(self, drag_c: float, rotor_k_per_kg: float = 0.25, process_noise: float = 0.3,
                 meas_noise: float = 0.5, force_tau: float = 0.5) -> None:
        self.c = drag_c
        self.k_per_kg = rotor_k_per_kg
        self.q = process_noise
        self.r = meas_noise
        self.force_tau = force_tau
        self.f_filt = np.zeros(3)
        self.wind = np.zeros(3)
        self.P = np.eye(3) * 25.0
        self.gust_var = 0.0
        self.samples = 0

    def air_velocity(self, force: np.ndarray, mass: float) -> np.ndarray:
        fm = norm(force)
        if fm < 1e-9:
            return np.zeros(3)
        k = self.k_per_kg * mass
        s = (-k + math.sqrt(k * k + 4.0 * self.c * fm)) / (2.0 * self.c)
        return -force / fm * s

    def update(self, v_ground: np.ndarray, accel: np.ndarray, thrust_accel: np.ndarray, mass: float, dt: float) -> None:
        f_raw = mass * (accel - thrust_accel - G_VEC)
        a = min(1.0, dt / self.force_tau)
        self.f_filt = self.f_filt + a * (f_raw - self.f_filt)
        z = v_ground - self.air_velocity(self.f_filt, mass)
        self.P += np.eye(3) * self.q * dt
        S = self.P + np.eye(3) * self.r
        K = self.P @ np.linalg.inv(S)
        innov = z - self.wind
        self.wind = self.wind + K @ innov
        self.P = (np.eye(3) - K) @ self.P
        b = min(1.0, dt / 5.0)
        h = innov[:2]
        self.gust_var = (1 - b) * self.gust_var + b * max(0.0, float(h @ h) / 2.0 - self.r * 0.3)
        self.samples += 1

    @property
    def speed(self) -> float:
        return float(np.hypot(self.wind[0], self.wind[1]))

    @property
    def gust_std(self) -> float:
        return math.sqrt(self.gust_var)

    @property
    def converged(self) -> bool:
        return self.samples > 60


class OpsLevel(IntEnum):
    NOMINAL = 0
    CAUTION = 1  # fly, monitor, minor speed reduction
    DEGRADED = 2  # fly slower/lower, re-weight sensors
    HOLD = 3  # do not launch / hold position or delay
    RETURN = 4  # abort the delivery, go home (or nearest depot)
    LAND_NOW = 5  # land at the nearest safe site immediately


@dataclass
class WeatherAssessment:
    level: OpsLevel
    speed_factor: float
    altitude_cap_agl: float | None
    sensor_weights: dict[str, float]
    heaters_on: bool
    margin: float  # 0 = at the envelope edge, 1 = benign
    reasons: list[str] = field(default_factory=list)

    @property
    def go(self) -> bool:
        return self.level <= OpsLevel.DEGRADED


def assess_weather(
    w: WeatherState,
    profile: VehicleProfile,
    airborne: bool = False,
    measured_wind: float | None = None,
    measured_gust_std: float | None = None,
    altitude_agl: float = 10.0,
) -> WeatherAssessment:
    """Graded operational decision for the current/forecast weather.

    The forecast wind (10 m reference) is evaluated at ``altitude_agl``, the
    height the vehicle actually flies. Pre-flight anything above DEGRADED means
    "do not launch" (HOLD). In flight the same conditions escalate to RETURN or
    LAND_NOW.
    """
    lim = profile.limits
    level = OpsLevel.NOMINAL
    speed = 1.0
    alt_cap: float | None = None
    reasons: list[str] = []
    ratios: list[float] = []
    abort = OpsLevel.RETURN if airborne else OpsLevel.HOLD

    def bump(new: OpsLevel, why: str) -> None:
        nonlocal level
        if new > level:
            level = new
        if new >= OpsLevel.CAUTION:
            reasons.append(why)

    # Lightning: never negotiable.
    if w.lightning_km < lim.lightning_land_km:
        bump(OpsLevel.LAND_NOW if airborne else OpsLevel.HOLD, f"lightning {w.lightning_km:.1f} km away")
    elif w.lightning_km < lim.lightning_standoff_km:
        bump(abort, f"lightning within {lim.lightning_standoff_km:.0f} km standoff")
    ratios.append(lim.lightning_standoff_km / max(w.lightning_km, 1e-3))

    # Icing.
    heaters = False
    if w.freezing_rain:
        bump(abort, "freezing rain: ice accretion exceeds de-icing capacity")
    elif w.icing_risk:
        if lim.icing_protection:
            heaters = True
            bump(OpsLevel.CAUTION, "icing conditions: prop/sensor heaters on")
        else:
            bump(abort, "icing conditions without icing protection")
    if w.temperature_c < 8.0 and profile.battery_heater:
        heaters = True

    # Wind: trust the worse of forecast and in-flight measurement.
    wind = max(wind_at_altitude(w.wind_speed, altitude_agl), measured_wind or 0.0)
    gust_std = max(w.gust_std, measured_gust_std or 0.0)
    peak = wind + 3.0 * gust_std
    r_wind = max(wind / lim.max_sustained_wind, peak / lim.max_gust)
    ratios.append(r_wind)
    if r_wind > 1.15 and airborne:
        bump(OpsLevel.LAND_NOW, f"wind {wind:.1f} m/s (gusts {peak:.1f}) far beyond envelope")
    elif r_wind > 1.0:
        bump(abort, f"wind {wind:.1f} m/s (gusts {peak:.1f}) beyond envelope")
    elif r_wind > 0.8:
        bump(OpsLevel.DEGRADED, f"strong wind {wind:.1f} m/s: slow down, fly lower")
        speed = min(speed, 0.7)
        alt_cap = max(profile.min_altitude_agl, altitude_agl - 20.0)
    elif r_wind > 0.6:
        bump(OpsLevel.CAUTION, f"breezy {wind:.1f} m/s")
        speed = min(speed, 0.85)

    # Precipitation.
    r_rain = w.rain_mm_h / lim.max_rain_mm_h
    r_snow = w.snow_mm_h / lim.max_snow_mm_h
    r_precip = max(r_rain, r_snow)
    ratios.append(r_precip)
    if r_precip > 1.0:
        bump(abort, f"precipitation beyond IP rating (rain {w.rain_mm_h:.0f} mm/h, snow {w.snow_mm_h:.0f} mm/h)")
    elif r_precip > 0.3:
        bump(OpsLevel.DEGRADED, "heavy precipitation: LiDAR/camera de-rated, radar primary")
        speed = min(speed, 0.8)
    elif r_precip > 0.0:
        bump(OpsLevel.CAUTION, "light precipitation")

    # Visibility (fog, smoke, heavy snow).
    ratios.append(lim.min_visibility_m / max(w.visibility_m, 1.0))
    if w.visibility_m < lim.min_visibility_m:
        bump(abort, f"visibility {w.visibility_m:.0f} m below minimum")
    elif w.visibility_m < 300.0:
        bump(OpsLevel.DEGRADED, f"fog: visibility {w.visibility_m:.0f} m, radar-led detect-and-avoid")
        speed = min(speed, 0.6)
    elif w.visibility_m < 1500.0:
        bump(OpsLevel.CAUTION, f"reduced visibility {w.visibility_m:.0f} m")
        speed = min(speed, 0.85)

    # Temperature.
    if not lim.min_temp_c <= w.temperature_c <= lim.max_temp_c:
        bump(abort, f"temperature {w.temperature_c:.0f} C outside {lim.min_temp_c:.0f}..{lim.max_temp_c:.0f} C")
    ratios.append(max((lim.min_temp_c + 30) / max(w.temperature_c + 30, 1e-3), w.temperature_c / lim.max_temp_c))

    # Darkness.
    if w.illumination_lux < 5.0:
        if profile.has_thermal_camera and profile.has_nir_illuminator:
            bump(OpsLevel.CAUTION, "night: thermal + NIR illumination")
        else:
            bump(OpsLevel.DEGRADED, "night without thermal/NIR: vision de-rated")
            speed = min(speed, 0.7)

    # Sensor de-rating (doc 1.11 "Adaptive Software").
    vis_f = min(1.0, w.visibility_m / 1000.0)
    light_f = min(1.0, w.illumination_lux / 50.0)
    if profile.has_thermal_camera:
        light_f = max(light_f, 0.6)
    weights = {
        "lidar": float(np.clip((1.0 - 0.6 * min(r_precip, 1.0)) * min(1.0, w.visibility_m / 300.0), 0.05, 1.0)),
        "camera": float(np.clip(vis_f * light_f * (1.0 - 0.3 * min(r_precip, 1.0)), 0.05, 1.0)),
        "vio": float(np.clip(min(1.0, w.visibility_m / 300.0) * max(light_f, 0.5) * (1.0 - 0.2 * min(r_precip, 1.0)), 0.05, 1.0)),
        "radar": 1.0 if profile.has_radar else 0.0,
        "gnss": 1.0,
    }

    margin = float(np.clip(1.0 - max(ratios), 0.0, 1.0))
    if level >= OpsLevel.HOLD:
        speed = 0.0 if not airborne else speed
    return WeatherAssessment(level, speed, alt_cap, weights, heaters, margin, reasons)


def with_overrides(w: WeatherState, **kw) -> WeatherState:
    return replace(w, **kw)
