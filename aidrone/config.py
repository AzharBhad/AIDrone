"""Vehicle profiles and the all-weather operational envelope (ODD).

"Any weather" is implemented as a *wide, graded* envelope rather than "no
limits": the vehicle is ruggedised (IP67, heated battery and sensors,
anti-icing props, radar for fog) so it keeps flying far past a typical
consumer drone, and the autonomy degrades gracefully - slower, lower, more
sensor redundancy - as conditions approach the certified limits. Beyond the
limits (lightning nearby, freezing rain, winds it cannot make headway in) the
safest delivery is the one that waits, and the stack says so explicitly.

Defaults mirror ``AIDroneOperations/Part1.../Delivery_Operations-Part1.yaml``
and ``Documentation/Custom recommendation.md``, extended for the all-weather
hardware described in ``Documentation/Process/1.11 Adverse Weather
Operations.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class WeatherLimits:
    max_sustained_wind: float = 15.0  # m/s at 10 m AGL (54 km/h)
    max_gust: float = 21.0  # m/s peak (mean + 3 sigma turbulence)
    max_rain_mm_h: float = 50.0  # IP67 airframe, hydrophobic optics
    max_snow_mm_h: float = 10.0
    min_temp_c: float = -20.0
    max_temp_c: float = 50.0
    min_visibility_m: float = 100.0  # radar + LiDAR detect-and-avoid
    lightning_standoff_km: float = 16.0  # ~10 statute miles: stay away / go home
    lightning_land_km: float = 8.0  # too close: land at nearest safe site now
    icing_protection: bool = True  # heated props/sensors permit light icing conditions


@dataclass(frozen=True)
class VehicleProfile:
    name: str = "urban_courier"
    empty_mass_kg: float = 14.0
    max_payload_kg: float = 5.0
    motor_count: int = 6  # hexacopter: tolerates a single motor failure
    max_thrust_to_weight: float = 2.2  # at max take-off mass, all motors healthy
    max_tilt_deg: float = 38.0
    max_airspeed: float = 22.0  # m/s
    cruise_speed: float = 15.0
    max_climb_rate: float = 5.0
    max_descent_rate: float = 3.0
    drag_area_m2: float = 0.12  # Cd * A, for quadratic drag
    rotor_drag_per_kg: float = 0.25  # linear blade-flapping/induced drag, N/(m/s) per kg
    battery_wh: float = 1600.0
    reserve_fraction: float = 0.20
    hover_power_w_per_kg: float = 180.0
    heater_power_w: float = 120.0
    battery_heater: bool = True
    has_radar: bool = True
    has_thermal_camera: bool = True
    has_nir_illuminator: bool = True
    has_parachute: bool = True
    has_winch: bool = True
    tether_length_m: float = 25.0
    min_altitude_agl: float = 30.0
    cruise_altitude_agl: float = 100.0
    max_altitude_agl: float = 120.0  # Part 107 / EU open-category ceiling
    safety_radius_m: float = 1.2  # half-span incl. props
    limits: WeatherLimits = field(default_factory=WeatherLimits)

    @property
    def max_takeoff_mass(self) -> float:
        return self.empty_mass_kg + self.max_payload_kg

    def drag_coefficient(self, air_density: float = 1.225) -> float:
        """c in F_drag = -(k + c |v_air|) v_air."""
        return 0.5 * air_density * self.drag_area_m2

    def rotor_drag(self, mass: float) -> float:
        """k in F_drag = -(k + c |v_air|) v_air (dominant at low airspeed)."""
        return self.rotor_drag_per_kg * mass


URBAN_COURIER = VehicleProfile()

HEAVY_LIFT_50KG = VehicleProfile(
    name="heavy_lift_50kg",
    empty_mass_kg=62.0,
    max_payload_kg=50.0,
    motor_count=8,
    max_thrust_to_weight=1.9,
    max_tilt_deg=30.0,
    max_airspeed=20.0,
    cruise_speed=13.0,
    max_climb_rate=4.0,
    max_descent_rate=2.5,
    drag_area_m2=0.45,
    battery_wh=12000.0,
    hover_power_w_per_kg=170.0,
    heater_power_w=400.0,
    safety_radius_m=2.2,
    tether_length_m=30.0,
    # Custom recommendation asks for ~300 m; that needs a waiver/SORA so it is
    # only the ceiling, the planner still prefers the cruise altitude.
    max_altitude_agl=300.0,
    cruise_altitude_agl=120.0,
    limits=WeatherLimits(max_sustained_wind=13.0, max_gust=19.0),
)

PROFILES = {p.name: p for p in (URBAN_COURIER, HEAVY_LIFT_50KG)}


def _build(cls, data: dict[str, Any]):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def profile_from_dict(data: dict[str, Any]) -> VehicleProfile:
    data = dict(data)
    base = PROFILES.get(data.pop("base", "urban_courier"), URBAN_COURIER)
    limits = data.pop("limits", None)
    if limits is not None:
        data["limits"] = _build(WeatherLimits, {**asdict(base.limits), **limits})
    unknown = set(data) - {f.name for f in fields(VehicleProfile)}
    if unknown:
        raise ValueError(f"unknown VehicleProfile keys: {sorted(unknown)}")
    return replace(base, **data) if data else base


def load_profile(path: str | Path) -> VehicleProfile:
    """Load a vehicle profile from YAML (see ``config/urban_all_weather.yaml``)."""
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return profile_from_dict(doc.get("vehicle", doc))
