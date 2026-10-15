import math

import numpy as np
import pytest

from aidrone.config import PROFILES, URBAN_COURIER, load_profile, profile_from_dict
from aidrone.flight_controller import SimFlightController, TrajectorySetpoint
from aidrone.geometry import enu_to_ned, ned_to_enu, vec
from aidrone.sim.dynamics import MultirotorDynamics


def test_enu_ned_roundtrip():
    v = vec(1.0, 2.0, 3.0)
    assert np.allclose(enu_to_ned(v), [2, 1, -3])
    assert np.allclose(ned_to_enu(enu_to_ned(v)), v)


def test_px4_setpoint_layout_uses_ned_and_nan():
    sp = TrajectorySetpoint(position=vec(10, 20, 30), yaw=0.0)
    d = sp.to_px4(123)
    assert d["position"] == [20.0, 10.0, -30.0]
    assert all(math.isnan(x) for x in d["velocity"])
    assert math.isclose(d["yaw"], math.pi / 2)  # facing East


def test_hover_and_offboard_heartbeat_failsafe():
    dyn = MultirotorDynamics(URBAN_COURIER, vec(0, 0, 0), 16.0)
    fc = SimFlightController(dyn)
    fc.arm()
    t = 0.0
    for _ in range(400):  # climb to 20 m
        fc.send_setpoint(TrajectorySetpoint(position=vec(0, 0, 20)), t)
        fc.step(t, 0.05, np.zeros(3))
        t += 0.05
    assert abs(dyn.p[2] - 20) < 0.5
    # Companion computer freezes: FC must hold position, then land.
    for _ in range(100):
        fc.step(t, 0.05, np.zeros(3))
        t += 0.05
    assert fc.failsafe == "offboard_lost" and abs(dyn.p[2] - 20) < 1.0
    for _ in range(800):
        fc.step(t, 0.05, np.zeros(3))
        t += 0.05
    assert dyn.p[2] < 1.0


def test_holds_position_in_wind_with_feedforward():
    dyn = MultirotorDynamics(URBAN_COURIER, vec(0, 0, 30), 16.0)
    dyn.on_ground = False
    fc = SimFlightController(dyn)
    fc.arm()
    wind = vec(10, 0, 0)
    fc.set_wind_feedforward(wind)
    t = 0.0
    for _ in range(600):
        fc.send_setpoint(TrajectorySetpoint(position=vec(0, 0, 30)), t)
        fc.step(t, 0.05, wind)
        t += 0.05
    assert np.linalg.norm(dyn.p - vec(0, 0, 30)) < 0.5


def test_hexacopter_survives_one_motor_quad_does_not():
    hexa = MultirotorDynamics(URBAN_COURIER, vec(0, 0, 0), 16.0)
    hexa.fail_motor()
    assert hexa.thrust_to_weight > 1.2
    from dataclasses import replace

    quad = MultirotorDynamics(replace(URBAN_COURIER, motor_count=4), vec(0, 0, 0), 16.0)
    quad.fail_motor()
    assert quad.thrust_to_weight == 0.0


def test_profiles_and_yaml(tmp_path):
    assert "heavy_lift_50kg" in PROFILES and PROFILES["heavy_lift_50kg"].max_payload_kg == 50
    f = tmp_path / "p.yaml"
    f.write_text("vehicle:\n  base: urban_courier\n  max_payload_kg: 3.0\n  limits:\n    max_sustained_wind: 12\n")
    p = load_profile(f)
    assert p.max_payload_kg == 3.0 and p.limits.max_sustained_wind == 12 and p.limits.max_gust == 21.0
    with pytest.raises(ValueError):
        profile_from_dict({"warp_drive": True})


def test_repo_config_loads():
    from pathlib import Path

    p = load_profile(Path(__file__).resolve().parents[1] / "config" / "urban_all_weather.yaml")
    assert p.has_radar and p.limits.icing_protection
