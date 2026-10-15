"""Precision landing and tethered (winch) delivery - doc 1.8.

Precision landing is a finite-state machine on top of the navigation filter,
which already fuses UWB ranges (approach stage) and AprilTag/QR fixes
(terminal stage). The controller adds the *decision* layer:

* descent rate scales down with turbulence,
* the vehicle only descends while laterally inside a funnel that tightens
  with height (so a gust never drives it into the pad edge),
* below the terminal height it needs a fresh visual fix, otherwise it holds
  and eventually goes around,
* at most N go-arounds, then the delivery is aborted (doc 4.delivery_zone).

The winch controller lowers the parcel from a safe hover height - the
urban answer to balconies, courtyards and crowded front gardens where a
multirotor should never land. It damps pendulum sway in gusty canyons by
moving the aircraft over the swinging parcel and detects touchdown and snags
from line tension.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from .config import VehicleProfile
from .geometry import vec


class LandingStage(Enum):
    DESCENT = "descent"
    TERMINAL = "terminal"
    FLARE = "flare"
    GO_AROUND = "go_around"
    TOUCHDOWN = "touchdown"
    ABORT = "abort"


@dataclass
class LandingCommand:
    position: np.ndarray  # setpoint (horizontal hold + altitude reference)
    velocity: np.ndarray  # feed-forward
    stage: LandingStage
    note: str = ""


class PrecisionLander:
    def __init__(
        self,
        profile: VehicleProfile,
        pad: np.ndarray,
        start_height: float = 20.0,
        terminal_height: float = 8.0,
        max_go_arounds: int = 3,
    ) -> None:
        self.profile = profile
        self.pad = np.asarray(pad, dtype=float)
        self.start_height = start_height
        self.terminal_height = terminal_height
        self.max_go_arounds = max_go_arounds
        self.stage = LandingStage.DESCENT
        self.go_arounds = 0
        self.hold_since: float | None = None
        self.last_tag_t = -1e9
        self.alt_ref: float | None = None
        self.require_tag = True

    def update(self, t: float, pos: np.ndarray, tag_seen: bool, gust_std: float, dt: float,
               nav_ok: bool = True, require_tag: bool = True, zone_clear: bool = True) -> LandingCommand:
        if tag_seen:
            self.last_tag_t = t
        h = pos[2] - self.pad[2]
        if self.alt_ref is None:
            self.alt_ref = pos[2]
        lateral = float(np.hypot(*(pos[:2] - self.pad[:2])))
        hold_xy = self.pad.copy()

        if self.stage in (LandingStage.TOUCHDOWN, LandingStage.ABORT):
            return LandingCommand(vec(*hold_xy[:2], self.alt_ref), np.zeros(3), self.stage)

        if self.stage == LandingStage.GO_AROUND:
            target = self.pad[2] + self.terminal_height + 4.0
            self.alt_ref = min(self.alt_ref + self.profile.max_climb_rate * 0.5 * dt, target)
            if pos[2] >= target - 0.5:
                self.stage = LandingStage.DESCENT
                self.hold_since = None
            return LandingCommand(vec(*hold_xy[:2], self.alt_ref), vec(0, 0, 1.0), self.stage, "climbing for retry")

        # Descent-rate schedule: fast high up, slow near the pad, slower in gusts.
        gust_f = float(np.clip(1.0 - gust_std / 6.0, 0.35, 1.0))
        if h > self.terminal_height:
            rate = 1.5 * gust_f
            self.stage = LandingStage.DESCENT
        elif h > 0.8:
            rate = 0.7 * gust_f
            self.stage = LandingStage.TERMINAL
        else:
            rate = 0.35
            self.stage = LandingStage.FLARE

        funnel = max(0.3, 0.1 * h)
        tag_fresh = (t - self.last_tag_t) < 1.5
        hold_reason = ""
        if self.stage == LandingStage.FLARE:
            pass  # committed: aborting within a metre of the ground is riskier than landing
        elif lateral > funnel:
            hold_reason = f"off-centre {lateral:.2f} m > funnel {funnel:.2f} m"
        elif 0.8 < h <= self.terminal_height and require_tag and not tag_fresh:
            # (below 0.8 m the marker fills/leaves the camera view: UWB + IMU finish the flare)
            hold_reason = "no fresh visual fix on marker"
        elif h <= self.terminal_height and not nav_ok:
            hold_reason = "navigation integrity below landing alert limit"
        elif not zone_clear and h > 0.8:
            hold_reason = "landing zone not clear"
        if hold_reason:
            self.hold_since = t if self.hold_since is None else self.hold_since
            held = t - self.hold_since
            if (held > 6.0 and h < self.terminal_height + 2.0) or held > 20.0:
                self.go_arounds += 1
                self.hold_since = None
                if self.go_arounds > self.max_go_arounds:
                    self.stage = LandingStage.ABORT
                    return LandingCommand(vec(*hold_xy[:2], self.alt_ref), np.zeros(3), self.stage,
                                          f"abort after {self.max_go_arounds} go-arounds ({hold_reason})")
                self.stage = LandingStage.GO_AROUND
                return LandingCommand(vec(*hold_xy[:2], self.alt_ref), np.zeros(3), self.stage, hold_reason)
            return LandingCommand(vec(*hold_xy[:2], self.alt_ref), np.zeros(3), self.stage, hold_reason)

        self.hold_since = None
        # Reference may go below the pad so thrust unloads on contact (land detector cue).
        self.alt_ref = max(self.alt_ref - rate * dt, self.pad[2] - 1.0)
        return LandingCommand(vec(*hold_xy[:2], self.alt_ref), vec(0, 0, -rate), self.stage)

    def touched_down(self) -> None:
        self.stage = LandingStage.TOUCHDOWN


class WinchStage(Enum):
    LOWERING = "lowering"
    ON_GROUND = "on_ground"
    RELEASED = "released"
    RETRACTING = "retracting"
    DONE = "done"
    SNAG_CUT = "snag_cut"


@dataclass
class WinchCommand:
    payout_rate: float  # m/s, positive = lowering
    drone_offset: np.ndarray  # horizontal shift of the hover setpoint for sway damping
    release: bool
    cut: bool
    stage: WinchStage
    note: str = ""


class WinchController:
    def __init__(self, drop_height: float, payload_mass: float, tether_length: float) -> None:
        self.drop_height = drop_height  # metres from hover height down to the drop surface
        self.payload_weight = payload_mass * 9.81
        self.tether_length = tether_length
        self.stage = WinchStage.LOWERING
        self.slack_since: float | None = None
        self.aborted = False

    def abort(self) -> None:
        """Reel the parcel back in without releasing it."""
        self.aborted = True
        if self.stage in (WinchStage.LOWERING, WinchStage.ON_GROUND):
            self.stage = WinchStage.RETRACTING

    def update(self, t: float, payout: float, tension: float, sway: np.ndarray, sway_rate: np.ndarray,
               dt: float, authorised: bool = True) -> WinchCommand:
        offset = np.zeros(3)
        # Sway damping: move the suspension point toward the swinging parcel (PD on sway).
        offset[:2] = 0.8 * sway[:2] + 0.6 * sway_rate[:2]

        if self.stage == WinchStage.LOWERING:
            remaining = self.drop_height - payout
            rate = 1.2 if remaining > 4.0 else 0.4
            if tension > 2.5 * self.payload_weight:
                self.stage = WinchStage.SNAG_CUT
                return WinchCommand(0.0, np.zeros(3), False, True, self.stage, "tether snag: cutting line")
            if payout > self.tether_length - 0.2:
                self.stage = WinchStage.SNAG_CUT
                return WinchCommand(0.0, np.zeros(3), False, True, self.stage, "ran out of tether before ground contact")
            if tension < 0.3 * self.payload_weight and payout > 0.6 * self.drop_height:
                self.slack_since = t if self.slack_since is None else self.slack_since
                if t - self.slack_since > 0.6:
                    self.stage = WinchStage.ON_GROUND
            else:
                self.slack_since = None
            return WinchCommand(rate, offset, False, False, self.stage)

        if self.stage == WinchStage.ON_GROUND:
            if self.aborted:
                self.stage = WinchStage.RETRACTING
                return WinchCommand(-1.5, offset, False, False, self.stage)
            if not authorised:
                return WinchCommand(0.0, offset, False, False, self.stage, "awaiting recipient authorisation")
            self.stage = WinchStage.RELEASED
            return WinchCommand(0.0, offset, True, False, self.stage, "parcel released on ground")

        if self.stage in (WinchStage.RELEASED, WinchStage.RETRACTING, WinchStage.SNAG_CUT):
            if self.stage == WinchStage.SNAG_CUT:
                return WinchCommand(0.0, np.zeros(3), False, False, self.stage)
            self.stage = WinchStage.RETRACTING
            if payout <= 0.05:
                self.stage = WinchStage.DONE
            return WinchCommand(-1.5, np.zeros(3), False, False, self.stage)

        return WinchCommand(0.0, np.zeros(3), False, False, self.stage)
