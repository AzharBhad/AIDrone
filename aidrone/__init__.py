"""AIDrone - all-weather, urban precision-delivery autonomy stack.

Companion-computer software for the PX4 + ROS 2 "split brain" architecture
described in ``Documentation/Process``: localisation, perception, weather
intelligence, planning, precision landing / winch delivery, security and the
safety supervisor, plus a closed-loop simulator to prove it.
"""

from .autonomy import AutonomyCore, Phase
from .config import HEAVY_LIFT_50KG, PROFILES, URBAN_COURIER, VehicleProfile, WeatherLimits, load_profile
from .weather import OpsLevel, WeatherState, WeatherTimeline, assess_weather

__version__ = "0.1.0"

__all__ = [
    "AutonomyCore",
    "Phase",
    "VehicleProfile",
    "WeatherLimits",
    "URBAN_COURIER",
    "HEAVY_LIFT_50KG",
    "PROFILES",
    "load_profile",
    "WeatherState",
    "WeatherTimeline",
    "OpsLevel",
    "assess_weather",
]
