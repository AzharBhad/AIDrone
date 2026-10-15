"""Small vector helpers shared by every module.

Frame convention: the autonomy stack works in a local ENU frame
(x = East, y = North, z = Up, metres) anchored at the city origin. PX4 uses
NED, so conversion helpers live here and are used only at the flight
controller boundary (see :mod:`aidrone.flight_controller`).
"""

from __future__ import annotations

import math

import numpy as np

GRAVITY = 9.80665
G_VEC = np.array([0.0, 0.0, -GRAVITY])
AIR_DENSITY = 1.225


def vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=float)


def norm(v: np.ndarray) -> float:
    return float(np.linalg.norm(v))


def unit(v: np.ndarray) -> np.ndarray:
    n = norm(v)
    return v / n if n > 1e-9 else np.zeros_like(v, dtype=float)


def clamp_norm(v: np.ndarray, max_norm: float) -> np.ndarray:
    n = norm(v)
    if n > max_norm and n > 1e-12:
        return v * (max_norm / n)
    return v


def horizontal(v: np.ndarray) -> np.ndarray:
    return np.array([v[0], v[1], 0.0])


def horizontal_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def enu_to_ned(v: np.ndarray) -> np.ndarray:
    """Convert an ENU vector to NED (PX4 TrajectorySetpoint frame)."""
    return np.array([v[1], v[0], -v[2]], dtype=float)


def ned_to_enu(v: np.ndarray) -> np.ndarray:
    """Convert a NED vector back to ENU. The mapping is its own inverse."""
    return np.array([v[1], v[0], -v[2]], dtype=float)


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi
