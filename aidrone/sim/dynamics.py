"""Point-mass multirotor with a PX4-like cascaded position/velocity loop.

Physics: thrust (limited by thrust-to-weight, motor health and tilt), gravity,
quadratic aerodynamic drag on the *air-relative* velocity (so wind matters),
ground contact and an optional parachute. The controller has a velocity
integrator plus wind feed-forward from the onboard wind estimator - which is
how the stack holds position in gusty street canyons.
"""

from __future__ import annotations

import math

import numpy as np

from ..config import VehicleProfile
from ..geometry import G_VEC, GRAVITY, clamp_norm, norm


class MultirotorDynamics:
    def __init__(self, profile: VehicleProfile, position: np.ndarray, mass: float, ground_fn=None, seed: int = 0) -> None:
        self.profile = profile
        self.p = np.asarray(position, dtype=float).copy()
        self.v = np.zeros(3)
        self.a = np.zeros(3)
        self.thrust_acc = -G_VEC.copy()
        self.mass = mass
        self.motors_ok = profile.motor_count
        self.c_drag = profile.drag_coefficient()
        self.wind_ff = np.zeros(3)
        self.v_int = np.zeros(3)
        self.armed = False
        self.parachute = False
        self.ground_fn = ground_fn or (lambda x, y: 0.0)
        self.on_ground = True
        self.max_impact_speed = 0.0
        self.collided = False
        self.rng = np.random.default_rng(seed)
        # cascaded gains
        self.kp = np.array([1.0, 1.0, 1.4])
        self.kv = np.array([2.2, 2.2, 3.0])
        self.ki = np.array([0.4, 0.4, 0.6])

    @property
    def thrust_to_weight(self) -> float:
        healthy = self.motors_ok / self.profile.motor_count
        # losing a motor also costs yaw authority/efficiency on the opposite arm
        eff = healthy ** 1.4 if self.motors_ok < self.profile.motor_count else 1.0
        if self.profile.motor_count <= 4 and self.motors_ok < self.profile.motor_count:
            eff = 0.0  # a quadrotor cannot stay controllable on 3 motors
        return self.profile.max_thrust_to_weight * self.profile.max_takeoff_mass / self.mass * eff

    def fail_motor(self) -> None:
        self.motors_ok = max(0, self.motors_ok - 1)

    def step(self, dt: float, sp, wind: np.ndarray) -> None:
        if self.parachute:
            v_air = self.v - wind
            drag = -0.5 * 1.225 * 6.0 * norm(v_air) * v_air  # ~6 m^2 canopy CdA
            self.a = G_VEC + drag / self.mass
            self._integrate(dt)
            return
        if not self.armed or sp is None:
            a_thr = np.zeros(3)
        else:
            a_thr = self._control(dt, sp)
        v_air = self.v - wind
        drag = -(self.profile.rotor_drag(self.mass) + self.c_drag * norm(v_air)) * v_air
        self.thrust_acc = a_thr
        self.a = a_thr + G_VEC + drag / self.mass + self.rng.normal(0, 0.05, 3)
        self._integrate(dt)

    def _control(self, dt: float, sp) -> np.ndarray:
        prof = self.profile
        v_sp = np.zeros(3) if sp.velocity is None else np.asarray(sp.velocity, dtype=float).copy()
        if sp.position is not None:
            v_sp = v_sp + self.kp * (np.asarray(sp.position) - self.p)
        v_xy = clamp_norm(v_sp[:2], prof.max_airspeed)
        v_sp = np.array([v_xy[0], v_xy[1], float(np.clip(v_sp[2], -prof.max_descent_rate, prof.max_climb_rate))])
        err = v_sp - self.v
        self.v_int = np.clip(self.v_int + err * dt, -3.0, 3.0)
        a_cmd = self.kv * err + self.ki * self.v_int
        if sp.acceleration is not None:
            a_cmd = a_cmd + sp.acceleration
        # Feed-forward the predicted drag from the estimated wind.
        v_air_est = self.v - self.wind_ff
        drag_est = -(self.profile.rotor_drag(self.mass) + self.c_drag * norm(v_air_est)) * v_air_est
        a_thr = a_cmd - G_VEC - drag_est / self.mass
        # Thrust limits: vertical priority, then tilt, then total.
        t_max = self.thrust_to_weight * GRAVITY
        az = float(np.clip(a_thr[2], 0.0, t_max))
        h_max = min(az * math.tan(math.radians(prof.max_tilt_deg)), math.sqrt(max(t_max**2 - az**2, 0.0)))
        hxy = clamp_norm(a_thr[:2], h_max)
        self.v_int = np.where(np.abs(a_thr - np.array([hxy[0], hxy[1], az])) > 0.5, self.v_int * 0.98, self.v_int)
        return np.array([hxy[0], hxy[1], az])

    def _integrate(self, dt: float) -> None:
        prev_z = self.p[2]
        self.v = self.v + self.a * dt
        self.p = self.p + self.v * dt
        g = self.ground_fn(self.p[0], self.p[1])
        if self.p[2] <= g:
            if prev_z < g - 0.3:
                self.collided = True  # flew sideways into a structure
            if not self.on_ground:
                self.max_impact_speed = max(self.max_impact_speed, norm(self.v))
            self.p[2] = g
            self.v = np.zeros(3)
            self.a = np.zeros(3)  # ground reaction: an IMU at rest reads no kinematic acceleration
            self.on_ground = True
            self.v_int[:] = 0.0
        else:
            self.on_ground = False
