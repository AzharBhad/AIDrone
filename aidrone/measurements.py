"""Sensor message types consumed by the onboard autonomy stack.

On the real aircraft these are filled from ROS 2 topics (sensor_msgs,
px4_msgs); in simulation by :mod:`aidrone.sim.sensors`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class ImuSample:
    accel: np.ndarray  # world-frame kinematic acceleration (gravity removed), m/s^2
    thrust_accel: np.ndarray  # thrust/mass from ESC telemetry, world frame


@dataclass
class GnssFix:
    position: np.ndarray
    sigma_h: float  # receiver-reported (can be optimistic under multipath!)
    sigma_v: float
    fix_type: str  # rtk_fixed | rtk_float | 3d | none
    satellites: int
    hdop: float


@dataclass
class VioMeasurement:
    velocity: np.ndarray
    sigma: float
    quality: float  # 0..1, tracked feature health


@dataclass
class BaroMeasurement:
    altitude: float
    sigma: float = 0.5


@dataclass
class UwbRanges:
    anchors: np.ndarray  # (K, 3)
    ranges: np.ndarray  # (K,)
    sigma: float = 0.1


@dataclass
class TagDetection:
    relative: np.ndarray  # pad position minus drone position, world frame
    sigma: float
    payload: bytes  # signed landing-marker payload read from the fiducial/QR


@dataclass
class RangefinderMeasurement:
    distance: float  # downward range to surface
    valid: bool = True


@dataclass
class RadarTrack:
    position: np.ndarray  # relative in SensorFrame; absolute once perception has fused it
    velocity: np.ndarray
    radius: float = 1.0


@dataclass
class CameraDetection:
    position: np.ndarray  # relative in SensorFrame; absolute once perception has fused it
    label: str  # person | vehicle | bird | drone | obstacle
    confidence: float


@dataclass
class SensorFrame:
    """Everything that arrived during one autonomy tick."""

    t: float
    imu: ImuSample
    gnss: GnssFix | None = None
    vio: VioMeasurement | None = None
    baro: BaroMeasurement | None = None
    uwb: UwbRanges | None = None
    tag: TagDetection | None = None
    rangefinder: RangefinderMeasurement | None = None
    # Exteroceptive returns are RELATIVE to the vehicle (world-aligned axes, i.e. already
    # rotated by attitude). Perception adds the *estimated* position, as on the aircraft.
    lidar_points: np.ndarray | None = None  # (M, 3)
    radar: list[RadarTrack] = field(default_factory=list)
    camera: list[CameraDetection] = field(default_factory=list)
    winch_tension_n: float | None = None
    winch_payout_m: float | None = None
    winch_sway: np.ndarray | None = None  # parcel offset below the aircraft (tether angle sensor)
    winch_sway_rate: np.ndarray | None = None
    link_ok: bool = True
