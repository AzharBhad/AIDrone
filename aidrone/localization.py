"""Hybrid GNSS-RTK / VIO / UWB / Vision / Baro navigation filter.

Implements ``Documentation/Process/1.3 Localization Fusion Stack.md``:

* GNSS-RTK primary in open sky, VIO in urban canyons, UWB + AprilTag for
  terminal guidance - all in one EKF so hand-overs are seamless;
* chi-square innovation gating rejects multipath/NLOS GNSS jumps and NLOS
  UWB ranges instead of letting them corrupt the state;
* per-sensor health scores and RAIM-style protection levels so the mission
  manager knows when the navigation solution can no longer be trusted for
  the current phase (cruise vs. landing alert limits).

State: [p(3), v(3), baro_bias].
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .measurements import BaroMeasurement, GnssFix, TagDetection, UwbRanges, VioMeasurement

# 99.9 % chi-square thresholds by degrees of freedom.
CHI2_999 = {1: 10.83, 2: 13.82, 3: 16.27}

ALERT_LIMITS = {  # horizontal alert limit (m) per flight phase
    "cruise": 10.0,
    "approach": 3.0,
    "landing": 0.5,
}


@dataclass
class SensorHealth:
    accepted: int = 0
    rejected: int = 0
    consecutive_rejects: int = 0
    score: float = 1.0  # EMA of acceptance
    last_used_t: float = -1e9

    def record(self, ok: bool, t: float) -> None:
        self.score = 0.9 * self.score + 0.1 * (1.0 if ok else 0.0)
        if ok:
            self.accepted += 1
            self.consecutive_rejects = 0
            self.last_used_t = t
        else:
            self.rejected += 1
            self.consecutive_rejects += 1


@dataclass
class NavSolution:
    position: np.ndarray
    velocity: np.ndarray
    cov: np.ndarray
    hpl: float  # horizontal protection level
    vpl: float
    sources: list[str]
    integrity_ok: bool
    alert_limit: float


@dataclass
class NavFilter:
    x: np.ndarray = field(default_factory=lambda: np.zeros(7))
    P: np.ndarray = field(default_factory=lambda: np.diag([4, 4, 4, 1, 1, 1, 1.0]))
    accel_noise: float = 0.6
    bias_walk: float = 0.02
    health: dict[str, SensorHealth] = field(default_factory=dict)
    t: float = 0.0
    phase: str = "cruise"
    resets: int = 0

    TRUSTED = ("gnss", "vio", "vision_tag")
    RESET_AFTER = 10

    def initialize(self, position: np.ndarray, baro_alt: float | None = None) -> None:
        self.x[:] = 0.0
        self.x[:3] = position
        if baro_alt is not None:
            self.x[6] = baro_alt - position[2]
        self.P = np.diag([0.25, 0.25, 0.25, 0.05, 0.05, 0.05, 0.5])

    @property
    def position(self) -> np.ndarray:
        return self.x[:3].copy()

    @property
    def velocity(self) -> np.ndarray:
        return self.x[3:6].copy()

    def _h(self, name: str) -> SensorHealth:
        return self.health.setdefault(name, SensorHealth())

    # ----------------------------------------------------------------- predict
    def predict(self, accel: np.ndarray, dt: float) -> None:
        F = np.eye(7)
        F[0:3, 3:6] = np.eye(3) * dt
        self.x[:3] += self.x[3:6] * dt + 0.5 * accel * dt * dt
        self.x[3:6] += accel * dt
        q = self.accel_noise**2
        G = np.zeros((7, 3))
        G[0:3] = np.eye(3) * 0.5 * dt * dt
        G[3:6] = np.eye(3) * dt
        Q = G @ G.T * q
        Q[6, 6] = self.bias_walk**2 * dt
        self.P = F @ self.P @ F.T + Q
        self.t += dt

    # ------------------------------------------------------------------ update
    def _update(self, name: str, z, h, H, R, dof: int | None = None) -> bool:
        z = np.atleast_1d(np.asarray(z, dtype=float))
        h = np.atleast_1d(np.asarray(h, dtype=float))
        H = np.atleast_2d(H)
        R = np.atleast_2d(R)
        y = z - h
        S = H @ self.P @ H.T + R
        Sinv = np.linalg.inv(S)
        nis = float(y @ Sinv @ y)
        ok = nis <= CHI2_999.get(dof or len(z), 16.27)
        health = self._h(name)
        health.record(ok, self.t)
        if not ok:
            if name in self.TRUSTED and health.consecutive_rejects >= self.RESET_AFTER:
                # A high-integrity sensor has disagreed for a sustained period: the
                # filter, not the sensor, is wrong. Inflate the affected states so it
                # re-converges instead of diverging (filter-divergence recovery).
                idx = np.nonzero(np.any(H != 0, axis=0))[0]
                for i in idx:
                    self.P[i, i] += float(np.max(np.abs(y))) ** 2 + 1.0
                health.consecutive_rejects = 0
                self.resets += 1
            return False
        K = self.P @ H.T @ Sinv
        self.x = self.x + K @ y
        I_KH = np.eye(7) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R @ K.T  # Joseph form: stays PSD
        return True

    def update_gnss(self, fix: GnssFix) -> bool:
        if fix.fix_type == "none":
            return False
        if fix.fix_type == "3d":
            # Standalone fixes in the city carry unmodelled multipath: de-weight them
            # and never let them trigger a divergence reset.
            H = np.zeros((3, 7))
            H[:, :3] = np.eye(3)
            R = np.diag([fix.sigma_h**2, fix.sigma_h**2, fix.sigma_v**2]) * 4.0
            return self._update("gnss_3d", fix.position, self.x[:3], H, R)
        H = np.zeros((3, 7))
        H[:, :3] = np.eye(3)
        R = np.diag([fix.sigma_h**2, fix.sigma_h**2, fix.sigma_v**2])
        # If repeated rejections, inflate R so a genuine re-convergence can happen
        # (but only after other sensors have kept the state sane).
        if self._h("gnss").score < 0.3:
            R = R * 4.0
        return self._update("gnss", fix.position, self.x[:3], H, R)

    def update_vio(self, m: VioMeasurement) -> bool:
        H = np.zeros((3, 7))
        H[:, 3:6] = np.eye(3)
        return self._update("vio", m.velocity, self.x[3:6], H, np.eye(3) * m.sigma**2)

    def update_baro(self, m: BaroMeasurement) -> bool:
        H = np.zeros((1, 7))
        H[0, 2] = 1.0
        H[0, 6] = 1.0
        return self._update("baro", m.altitude, self.x[2] + self.x[6], H, [[m.sigma**2]])

    def update_uwb(self, m: UwbRanges) -> int:
        used = 0
        for a, r in zip(m.anchors, m.ranges):
            d = self.x[:3] - a
            pred = float(np.linalg.norm(d))
            if pred < 1e-3:
                continue
            H = np.zeros((1, 7))
            H[0, :3] = d / pred
            if self._update("uwb", r, pred, H, [[m.sigma**2]], dof=1):
                used += 1
        return used

    def update_tag(self, m: TagDetection, pad_position: np.ndarray) -> bool:
        """Fiducial gives pad-relative position; with a surveyed pad that is absolute."""
        H = np.zeros((3, 7))
        H[:, :3] = np.eye(3)
        z = pad_position - m.relative
        return self._update("vision_tag", z, self.x[:3], H, np.eye(3) * m.sigma**2)

    # --------------------------------------------------------------- integrity
    def solution(self) -> NavSolution:
        Ph = self.P[:2, :2]
        hpl = 6.0 * float(np.sqrt(max(np.linalg.eigvalsh(Ph).max(), 0.0)))
        vpl = 5.33 * float(np.sqrt(max(self.P[2, 2], 0.0)))
        recent = [k for k, h in self.health.items() if self.t - h.last_used_t < 1.0]
        al = ALERT_LIMITS.get(self.phase, 10.0)
        return NavSolution(self.position, self.velocity, self.P.copy(), hpl, vpl, sorted(recent), hpl <= al, al)

    def mode(self) -> str:
        s = set(self.solution().sources)
        if "vision_tag" in s or "uwb" in s:
            return "terminal (UWB/vision)"
        gnss = "gnss" in s or "gnss_3d" in s
        if gnss and "vio" in s:
            return "GNSS+VIO" if "gnss" in s else "VIO+standalone GNSS (urban canyon)"
        if gnss:
            return "GNSS"
        if "vio" in s:
            return "VIO (GNSS-denied)"
        return "inertial dead-reckoning"
