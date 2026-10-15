"""The onboard brain: mission manager + safety arbitration over every module.

One :meth:`AutonomyCore.tick` per sensor frame (20 Hz):

    sense -> localise (EKF + integrity) -> estimate wind -> assess weather
          -> perceive (diverse, weather-weighted) -> supervise (failsafes)
          -> mission phase logic -> plan (global A* / local DWA) -> setpoint

Every decision is written to an explainable event log ("AI decision
traces", doc 1.2 ground responsibilities) so operators and certification
reviewers can see *why* the aircraft did what it did.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .citymap import CityMap, DeliverySite
from .config import VehicleProfile
from .fleet import DeliveryTicket
from .flight_controller import FlightControllerInterface, TrajectorySetpoint
from .geometry import horizontal_distance, norm, vec
from .landing import LandingStage, PrecisionLander, WinchCommand, WinchController, WinchStage
from .localization import NavFilter, NavSolution
from .measurements import SensorFrame
from .perception import PerceivedWorld, PerceptionFusion, landing_zone_clear, touchdown_votes
from .planning import GlobalPlanner, LocalPlanner, PathTracker
from .safety import Battery, EnergyModel, SafetyAction, SafetyDecision, SafetySupervisor, VehicleHealth
from .security import MarkerVerifier, verify_one_time_code
from .weather import OpsLevel, WeatherAssessment, WeatherState, WeatherTimeline, WindEstimator, assess_weather, wind_at_altitude


class Phase(Enum):
    PREFLIGHT = "preflight"
    REJECTED = "rejected"  # no launch: outside envelope / infeasible
    TAKEOFF = "takeoff"
    OUTBOUND = "outbound"
    APPROACH = "approach"
    ZONE_CHECK = "zone_check"
    DESCENT = "descent"
    LANDED_AT_SITE = "landed_at_site"
    WINCH = "winch"
    CLIMB_OUT = "climb_out"
    RETURN = "return"
    HOME_LANDING = "home_landing"
    DIVERT = "divert"
    DIVERT_LANDING = "divert_landing"
    EMERGENCY_LAND = "emergency_land"
    PARACHUTE = "parachute"
    COMPLETE = "complete"
    LANDED_ELSEWHERE = "landed_elsewhere"


TERMINAL_PHASES = {Phase.REJECTED, Phase.COMPLETE, Phase.LANDED_ELSEWHERE, Phase.PARACHUTE}


@dataclass
class Event:
    t: float
    phase: str
    category: str
    message: str


@dataclass
class AutonomyOutput:
    setpoint: TrajectorySetpoint
    winch: WinchCommand | None = None
    release_payload: bool = False
    parachute: bool = False


@dataclass
class MissionStatus:
    delivered: bool = False
    delivery_method: str | None = None
    delivery_error_m: float | None = None
    abort_reason: str | None = None
    replans: int = 0
    go_arounds: int = 0
    security_rejections: list[str] = field(default_factory=list)


class AutonomyCore:
    def __init__(
        self,
        profile: VehicleProfile,
        hd_map: CityMap,
        verifier: MarkerVerifier,
        fc: FlightControllerInterface,
        battery: Battery,
        weather_feed: WeatherTimeline,
        forecast: WeatherTimeline | None = None,
    ) -> None:
        self.profile = profile
        self.map = hd_map
        self.verifier = verifier
        self.fc = fc
        self.battery = battery
        self.weather_feed = weather_feed  # live nowcast over the C2 link
        self.forecast = forecast or weather_feed  # what dispatch knew at launch
        self.nav = NavFilter()
        self.wind_est = WindEstimator(profile.drag_coefficient(), profile.rotor_drag_per_kg)
        self.perception = PerceptionFusion(hd_map)
        self.planner = GlobalPlanner(hd_map, profile)
        self.inflated = self.planner.set_clearance(profile.safety_radius_m + 4.0)
        self.local = LocalPlanner(self.inflated, profile)
        self.supervisor = SafetySupervisor(hd_map, profile)
        self.energy = EnergyModel(profile)
        self.events: list[Event] = []
        self._last_logged: dict[tuple, float] = {}
        self.status = MissionStatus()
        self.phase = Phase.PREFLIGHT
        self.t = 0.0
        self.mass = profile.empty_mass_kg
        self.payload_kg = 0.0
        self.ticket: DeliveryTicket | None = None
        self.site: DeliverySite | None = None
        self.home = hd_map.depot.position.copy()
        self.tracker: PathTracker | None = None
        self.lander: PrecisionLander | None = None
        self.winch: WinchController | None = None
        self.hover_point: np.ndarray | None = None
        self.marker_verified_for: str | None = None
        self.zone_attempts = 0
        self.zone_wait_since: float | None = None
        self.zone_blocked_since: float | None = None
        self.approach_start: float | None = None
        self.release_wait_since: float | None = None
        self.divert_site: np.ndarray | None = None
        self.weather: WeatherAssessment | None = None
        self.world_view = PerceivedWorld(np.zeros((0, 3)))
        self.safety: SafetyDecision | None = None
        self.last_energy_eval = -1e9
        self.energy_needed = 0.0
        self.progress_mark: tuple[float, float] | None = None
        self.hold_since: float | None = None
        self.last_link_t = 0.0
        self.recipient_code_fn = None
        self.health = VehicleHealth(profile.motor_count, profile.motor_count, profile.max_thrust_to_weight, 0.0, 0.0, 0.0)
        self.last_tag_seen = False
        self.weather_bad_since: float | None = None
        self.cruise_alt = profile.cruise_altitude_agl

    # ================================================================ logging
    def log(self, category: str, message: str) -> None:
        # Rate-limit repeats of the same message shape (numbers stripped) to keep the trace readable.
        key = (category, self.phase, re.sub(r"[-\d.]+", "#", message))
        last = self._last_logged.get(key)
        if last is not None and self.t - last < 15.0:
            return
        self._last_logged[key] = self.t
        self.events.append(Event(round(self.t, 2), self.phase.value, category, message))

    def set_phase(self, phase: Phase, why: str = "") -> None:
        if phase != self.phase:
            self.phase = phase
            self.log("phase", f"-> {phase.value}" + (f" ({why})" if why else ""))

    # ============================================================== preflight
    def preflight(self, ticket: DeliveryTicket, t: float, initial_baro: float | None = None, recipient_code_fn=None) -> bool:
        """Go/no-go: weather over the mission window, route, energy, authentication."""
        self.t = t
        self.ticket = ticket
        self.recipient_code_fn = recipient_code_fn
        self.site = self.map.sites[ticket.order.site_id]
        self.payload_kg = ticket.order.payload_kg
        self.mass = self.profile.empty_mass_kg + self.payload_kg
        self.nav.initialize(self.home, initial_baro)
        if self.payload_kg > self.profile.max_payload_kg:
            return self._reject(f"payload {self.payload_kg} kg exceeds {self.profile.max_payload_kg} kg")
        if self.site.kind == "winch" and not self.profile.has_winch:
            return self._reject("site requires winch delivery but vehicle has no winch")

        w_now = self.forecast.at(t)
        alt, a = self._choose_cruise_altitude(w_now)
        self.weather = a
        self.log("weather", f"now: {a.level.name} margin {a.margin:.2f} at {alt:.0f} m AGL"
                 + (f" - {'; '.join(a.reasons)}" if a.reasons else ""))
        if not a.go:
            return self._reject("weather no-go: " + "; ".join(a.reasons))
        if alt < self.profile.cruise_altitude_agl:
            self.log("weather", f"winds aloft: cruising lower at {alt:.0f} m instead of {self.profile.cruise_altitude_agl:.0f} m")

        self.cruise_alt = alt
        self._configure_clearance(w_now.gust_std)
        self.hover_point = self._hover_point(self.site)
        plan_out = self.planner.plan(vec(*self.home[:2], self.cruise_alt), self._approach_point(self.site),
                                     w_now.wind_speed, self.cruise_alt)
        if plan_out is None:
            return self._reject("no safe route to destination")
        plan_back = self.planner.plan(self._approach_point(self.site), vec(*self.home[:2], self.cruise_alt),
                                      w_now.wind_speed, self.cruise_alt)
        if plan_back is None:
            return self._reject("no safe route home")
        wind_fn = self._wind_fn(w_now)
        speed = self.profile.cruise_speed * a.speed_factor
        e_out, t_out = self.energy.path([self.home] + plan_out.waypoints + [self.hover_point], self.mass, wind_fn, speed,
                                        w_now.gust_std, a.heaters_on)
        e_back, t_back = self.energy.path([self.hover_point] + plan_back.waypoints + [self.home], self.profile.empty_mass_kg,
                                          wind_fn, speed, w_now.gust_std, a.heaters_on)
        e_hover = self.energy.power(self.mass, 0.0, w_now.gust_std, a.heaters_on) * 120.0 / 3600.0
        need = e_out + e_back + e_hover
        usable = self.energy.usable_wh(w_now.temperature_c)
        reserve = self.profile.reserve_fraction * usable
        self.log("energy", f"mission needs {need:.0f} Wh of {usable:.0f} usable (+{reserve:.0f} reserve), "
                           f"route {plan_out.length:.0f}+{plan_back.length:.0f} m")
        if not math.isfinite(need) or need + reserve > min(usable, self.battery.energy_wh):
            return self._reject(f"insufficient energy: need {need:.0f}+{reserve:.0f} Wh")

        # The whole mission must fit in the weather window, not just the launch.
        mission_s = t_out + t_back + 240.0
        for w in self.forecast.worst_between(t, t + mission_s, step=30.0):
            fa = assess_weather(w, self.profile, airborne=False, altitude_agl=self.cruise_alt)
            if not fa.go:
                return self._reject("forecast deteriorates during mission: " + "; ".join(fa.reasons))

        self.tracker = PathTracker(plan_out.waypoints)
        self.log("plan", f"outbound {len(plan_out.waypoints)} waypoints, {plan_out.length:.0f} m, "
                         f"cruise {self.cruise_alt:.0f} m AGL, ETA {t_out:.0f}s")
        self.fc.arm()
        self.set_phase(Phase.TAKEOFF, "all pre-flight checks passed")
        return True

    def _choose_cruise_altitude(self, w: WeatherState) -> tuple[float, WeatherAssessment]:
        """Highest cruise altitude with the most benign assessment (wind grows with height)."""
        p = self.profile
        candidates = sorted({p.cruise_altitude_agl, 80.0, 60.0, p.min_altitude_agl + 20.0}, reverse=True)
        candidates = [c for c in candidates if p.min_altitude_agl <= c <= p.cruise_altitude_agl]
        options = [(alt, assess_weather(w, p, airborne=False, altitude_agl=alt)) for alt in candidates]
        # Least severe level first; once degraded, buy envelope margin by flying lower;
        # otherwise fly high (clear of most buildings, fewer detours, better GNSS sky view).
        def key(o):
            lvl = o[1].level
            return (lvl, -round(o[1].margin, 2) if lvl >= OpsLevel.DEGRADED else 0.0, -o[0])

        return min(options, key=key)

    def _reject(self, why: str) -> bool:
        self.status.abort_reason = why
        self.set_phase(Phase.REJECTED, why)
        return False

    def _configure_clearance(self, gust_std: float) -> None:
        lateral = self.profile.safety_radius_m + 4.0 + 1.5 * gust_std
        self.inflated = self.planner.set_clearance(lateral, 4.0 + gust_std)
        self.local.inflated = self.inflated

    def _wind_fn(self, w: WeatherState):
        ref = w.wind_vector()
        if self.wind_est.converged:
            ref = self.wind_est.wind.copy()
            ref[2] = 0.0

        def fn(p):
            return ref * wind_at_altitude(1.0, p[2]) if not self.wind_est.converged else ref

        return fn

    def _approach_point(self, site: DeliverySite) -> np.ndarray:
        return vec(site.position[0], site.position[1], max(self.cruise_alt, site.position[2] + 25.0))

    def _hover_point(self, site: DeliverySite) -> np.ndarray:
        """Lowest safe hover height above the drop point with a clear vertical column."""
        hover_agl = site.winch_hover_agl if site.kind == "winch" else 20.0
        z = site.position[2] + hover_agl
        r = self.profile.safety_radius_m + 1.5
        ring = [vec(math.cos(a) * r, math.sin(a) * r, 0) for a in np.linspace(0, 2 * math.pi, 12, endpoint=False)]
        top = self._approach_point(site)[2]
        while z < top:
            col = np.array([site.position + vec(0, 0, zz - site.position[2]) + o
                            for zz in np.arange(z, top, 2.0) for o in ring])
            if not self.map.occupied(col, include_unmapped=False).any():
                break
            z += 2.0
        if site.kind == "winch":
            z = min(z, site.position[2] + self.profile.tether_length_m - 2.0)
        return vec(site.position[0], site.position[1], z)

    # =================================================================== tick
    def tick(self, f: SensorFrame, dt: float, health: VehicleHealth) -> AutonomyOutput:
        self.t = f.t
        self.health = health
        if f.link_ok:
            self.last_link_t = f.t
        health.link_age_s = f.t - self.last_link_t

        self._localize(f, dt)
        nav = self.nav.solution()
        pos, vel = nav.position, nav.velocity
        airborne = self.phase not in (Phase.PREFLIGHT, Phase.REJECTED, Phase.COMPLETE, Phase.LANDED_ELSEWHERE)
        if airborne and pos[2] - self.map.ground_level(pos[0], pos[1]) > 3.0:
            self.wind_est.update(vel, f.imu.accel, f.imu.thrust_accel, self.mass, dt)
        self.fc.set_wind_feedforward(self.wind_est.wind if self.wind_est.converged else np.zeros(3))

        w = self.weather_feed.at(f.t) if f.link_ok else self.weather_feed.at(self.last_link_t)
        self.weather = assess_weather(
            w, self.profile, airborne=True, altitude_agl=max(pos[2] - self.map.ground_level(pos[0], pos[1]), 2.0),
            measured_wind=self.wind_est.speed if self.wind_est.converged else None,
            measured_gust_std=self.wind_est.gust_std if self.wind_est.converged else None,
        )
        self._debounce_weather()
        self.world_view = self.perception.update(f, pos, self.weather.sensor_weights)

        if self.phase in TERMINAL_PHASES:
            return AutonomyOutput(TrajectorySetpoint(velocity=vec(0, 0, -0.5)))

        self._update_energy_need(pos, w)
        self.safety = self.supervisor.evaluate(
            f.t, pos, vel, nav, self.weather, self.battery, self.energy.usable_wh(w.temperature_c),
            self.energy_needed, health, self.mass, self.wind_est.wind if self.wind_est.converged else w.wind_vector(),
            returning=self.phase in (Phase.CLIMB_OUT, Phase.RETURN, Phase.HOME_LANDING),
        )
        out = self._arbitrate(pos, vel)
        if out is not None:
            return out
        return self._run_phase(f, dt, nav)

    def _debounce_weather(self, persist_s: float = 8.0) -> None:
        """Wind-driven aborts need a sustained exceedance; a single gust is what the margin is for.

        Lightning, icing, temperature and visibility escalations stay immediate.
        """
        a = self.weather
        if a.level < OpsLevel.HOLD:
            self.weather_bad_since = None
            return
        immediate = ("lightning", "freezing", "icing", "temperature", "visibility", "precipitation beyond")
        if any(k in r for r in a.reasons for k in immediate):
            return
        self.weather_bad_since = self.t if self.weather_bad_since is None else self.weather_bad_since
        if self.t - self.weather_bad_since < persist_s:
            a.level = OpsLevel.DEGRADED
            a.speed_factor = min(a.speed_factor, 0.6)

    # ---------------------------------------------------------- localisation
    def _localize(self, f: SensorFrame, dt: float) -> None:
        nav = self.nav
        nav.predict(f.imu.accel, dt)
        if f.gnss is not None:
            nav.update_gnss(f.gnss)
        if f.vio is not None:
            nav.update_vio(f.vio)
        if f.baro is not None:
            nav.update_baro(f.baro)
        if f.uwb is not None:
            nav.update_uwb(f.uwb)
        self.last_tag_seen = False
        self.last_tag = f.tag
        if f.tag is not None:
            target = self._tag_reference()
            if target is not None:
                observed = nav.position + f.tag.relative
                if horizontal_distance(observed, target) < 3.0:
                    nav.update_tag(f.tag, target)
                    self.last_tag_seen = True
        if self.phase in (Phase.DESCENT, Phase.HOME_LANDING) and self.lander and nav.position[2] - self.lander.pad[2] < 8.0:
            nav.phase = "landing"
        elif self.phase in (Phase.APPROACH, Phase.ZONE_CHECK, Phase.DESCENT, Phase.HOME_LANDING, Phase.WINCH):
            nav.phase = "approach"
        else:
            nav.phase = "cruise"

    def _tag_reference(self) -> np.ndarray | None:
        """Only fuse a marker that has been cryptographically verified (or the home pad)."""
        if self.phase in (Phase.HOME_LANDING, Phase.TAKEOFF):
            return self.home
        if self.site is not None and self.marker_verified_for == self.site.site_id:
            return self.site.position
        return None

    # ---------------------------------------------------------------- energy
    def _update_energy_need(self, pos: np.ndarray, w: WeatherState) -> None:
        if self.t - self.last_energy_eval < 2.0:
            return
        self.last_energy_eval = self.t
        wind_fn = self._wind_fn(w)
        speed = self.profile.cruise_speed * (self.weather.speed_factor if self.weather else 1.0)
        heaters = bool(self.weather and self.weather.heaters_on)
        cruise = vec(0, 0, self.cruise_alt)

        def via_cruise(a, b, mass):
            pts = [a, vec(a[0], a[1], max(a[2], cruise[2])), vec(b[0], b[1], max(b[2], cruise[2]) ), b]
            e, _ = self.energy.path(pts, mass, wind_fn, speed, w.gust_std, heaters)
            return e * 1.25  # routing detour allowance

        target = self.divert_site if self.phase in (Phase.DIVERT, Phase.DIVERT_LANDING) else None
        if target is not None:
            self.energy_needed = via_cruise(pos, target, self.mass)
        elif self.payload_kg > 0 and self.site is not None and self.phase in (Phase.TAKEOFF, Phase.OUTBOUND, Phase.APPROACH,
                                                                                   Phase.ZONE_CHECK, Phase.DESCENT, Phase.WINCH):
            e_hover = self.energy.power(self.mass, 0, w.gust_std, heaters) * 90 / 3600
            self.energy_needed = via_cruise(pos, self.hover_point, self.mass) + e_hover + via_cruise(
                self.hover_point, self.home, self.profile.empty_mass_kg)
        else:
            self.energy_needed = via_cruise(pos, self.home, self.mass)

    # ------------------------------------------------------------ arbitration
    def _arbitrate(self, pos: np.ndarray, vel: np.ndarray) -> AutonomyOutput | None:
        s = self.safety
        if s is None or s.action == SafetyAction.CONTINUE:
            for r in (s.reasons if s else []):
                self.log("safety", r)
            self.hold_since = None
            return None
        for r in s.reasons:
            self.log("safety", f"L{int(s.level)} {s.action.name}: {r}")

        if s.action == SafetyAction.PARACHUTE:
            if self.phase != Phase.PARACHUTE:
                self.fc.deploy_parachute()
                self.status.abort_reason = "; ".join(s.reasons)
                self.set_phase(Phase.PARACHUTE, "flight termination")
            return AutonomyOutput(TrajectorySetpoint(velocity=np.zeros(3)), parachute=True)

        landing_phases = (Phase.DIVERT, Phase.DIVERT_LANDING, Phase.EMERGENCY_LAND)
        if s.action == SafetyAction.EMERGENCY_LAND and self.phase not in landing_phases:
            self.status.abort_reason = "; ".join(s.reasons)
            self.lander = PrecisionLander(self.profile, vec(pos[0], pos[1], self.map.ground_level(pos[0], pos[1])))
            self.set_phase(Phase.EMERGENCY_LAND, "; ".join(s.reasons))
        elif s.action == SafetyAction.DIVERT_LAND and self.phase not in landing_phases:
            if self.phase in (Phase.DESCENT, Phase.WINCH, Phase.LANDED_AT_SITE) and self.site is not None \
                    and self.map.population_at(*self.site.position[:2]) < 0.7 and self.site.kind == "pad":
                return None  # already landing at a surveyed pad - finishing that is the safest option
            self.status.abort_reason = "; ".join(s.reasons)
            self.divert_site = s.landing_site
            if self.divert_site is None:
                self.lander = PrecisionLander(self.profile, vec(pos[0], pos[1], self.map.ground_level(pos[0], pos[1])))
                self.set_phase(Phase.EMERGENCY_LAND, "no reachable contingency site")
            else:
                self._plan_to(pos, vec(self.divert_site[0], self.divert_site[1], max(self.divert_site[2] + 25, 40.0)),
                              "contingency site")
                self.set_phase(Phase.DIVERT, f"contingency landing at {self.divert_site[:2].round(0).tolist()}")
        elif s.action == SafetyAction.RETURN and self.phase not in landing_phases + (Phase.RETURN, Phase.HOME_LANDING):
            if self.phase in (Phase.DESCENT, Phase.LANDED_AT_SITE) and self.payload_kg == 0:
                return None
            if self.phase == Phase.WINCH and self.winch is not None:
                if self.winch.stage == WinchStage.LOWERING:
                    self.winch.abort()
                    self.log("winch", "abort: reeling parcel back in before returning")
                return None  # the winch phase finishes retracting, then climbs out
            if self.status.abort_reason is None:
                self.status.abort_reason = "; ".join(s.reasons)
            self.lander = None
            self.set_phase(Phase.CLIMB_OUT, "abort delivery, returning to base")
        elif s.action == SafetyAction.HOLD:
            self.hold_since = self.t if self.hold_since is None else self.hold_since
            if self.t - self.hold_since > 30.0 and self.phase not in (Phase.RETURN, Phase.HOME_LANDING):
                self.set_phase(Phase.CLIMB_OUT, "hold timed out")
                self.hold_since = None
            elif self.phase in (Phase.OUTBOUND, Phase.RETURN, Phase.DIVERT):
                return AutonomyOutput(TrajectorySetpoint(position=pos.copy(), velocity=np.zeros(3)))
        return None

    # ============================================================ phase logic
    def _run_phase(self, f: SensorFrame, dt: float, nav: NavSolution) -> AutonomyOutput:
        pos, vel = nav.position, nav.velocity
        ph = self.phase
        speed_factor = min(self.weather.speed_factor if self.weather else 1.0, self.safety.speed_factor if self.safety else 1.0)

        if ph == Phase.TAKEOFF:
            target = vec(self.home[0], self.home[1], self.cruise_alt)
            if pos[2] > self.cruise_alt - 1.5:
                self.set_phase(Phase.OUTBOUND, f"reached {self.cruise_alt:.0f} m, navigating ({self.nav.mode()})")
            return AutonomyOutput(TrajectorySetpoint(position=target, velocity=vec(0, 0, self.profile.max_climb_rate * 0.7)))

        if ph in (Phase.OUTBOUND, Phase.RETURN, Phase.DIVERT):
            return self._cruise(pos, vel, speed_factor)

        if ph == Phase.APPROACH:
            return self._approach(pos, vel)

        if ph == Phase.ZONE_CHECK:
            return self._zone_check(f, pos)

        if ph in (Phase.DESCENT, Phase.HOME_LANDING, Phase.DIVERT_LANDING, Phase.EMERGENCY_LAND):
            return self._land(f, dt, nav)

        if ph == Phase.LANDED_AT_SITE:
            return self._release_on_pad(pos)

        if ph == Phase.WINCH:
            return self._winch(f, dt, pos)

        if ph == Phase.CLIMB_OUT:
            target = vec(pos[0], pos[1], self.cruise_alt)
            if pos[2] >= self.cruise_alt - 1.5:
                if self._plan_to(pos, vec(self.home[0], self.home[1], self.cruise_alt), "home"):
                    self.set_phase(Phase.RETURN)
                else:
                    self.set_phase(Phase.EMERGENCY_LAND, "no route home")
            return AutonomyOutput(TrajectorySetpoint(position=target, velocity=vec(0, 0, 2.0)))

        return AutonomyOutput(TrajectorySetpoint(position=pos.copy(), velocity=np.zeros(3)))

    def _plan_to(self, pos: np.ndarray, goal: np.ndarray, label: str) -> bool:
        w = self.weather_feed.at(self.t)
        self._configure_clearance(max(w.gust_std, self.wind_est.gust_std if self.wind_est.converged else 0.0))
        start = pos.copy()
        prefix = [pos.copy()]
        if self.inflated.blocked(start[None, :])[0]:
            # Inside a canyon/courtyard: climb vertically clear of the inflated map first.
            start = vec(pos[0], pos[1], max(pos[2], self.cruise_alt))
            prefix.append(start)
        res = self.planner.plan(start, goal, w.wind_speed, self.cruise_alt)
        if res is None:
            self.log("plan", f"no route to {label}")
            return False
        self.tracker = PathTracker(prefix + res.waypoints[1:])
        self.progress_mark = None
        self.log("plan", f"route to {label}: {res.length:.0f} m, {len(res.waypoints)} waypoints")
        return True

    def _cruise(self, pos, vel, speed_factor) -> AutonomyOutput:
        assert self.tracker is not None
        remaining = self.tracker.remaining_distance(pos)
        v_max = self.profile.cruise_speed * speed_factor
        carrot = self.tracker.carrot(pos, max(10.0, 1.5 * norm(vel)))
        a_max = self._effective_accel()
        cmd = self.local.command(pos, vel, carrot, v_max, a_max, self.world_view, goal_dist=remaining)

        # Progress watchdog: blocked by something the HD map did not know about -> replan around it.
        if self.progress_mark is None or self.t - self.progress_mark[0] > 6.0:
            if self.progress_mark is not None and self.progress_mark[1] - remaining < 3.0 and remaining > 8.0:
                self._replan_around_obstacles(pos)
            self.progress_mark = (self.t, remaining)
        elif cmd.blocked and len(self.world_view.static_points):
            self._replan_around_obstacles(pos)
            self.progress_mark = (self.t, remaining)

        if remaining < 4.0 and norm(vel) < 2.5:
            if self.phase == Phase.OUTBOUND:
                self.approach_start = self.t
                self.set_phase(Phase.APPROACH, f"over destination, descending to {self.hover_point[2]:.0f} m")
            elif self.phase == Phase.RETURN:
                self.lander = PrecisionLander(self.profile, self.home)
                self.set_phase(Phase.HOME_LANDING)
            elif self.phase == Phase.DIVERT:
                self.lander = PrecisionLander(self.profile, self.divert_site)
                self.set_phase(Phase.DIVERT_LANDING)
        return AutonomyOutput(TrajectorySetpoint(velocity=cmd.velocity))

    def _effective_accel(self) -> float:
        """Acceleration authority left after holding the vehicle against wind."""
        g = 9.81
        t_max = self.profile.max_thrust_to_weight * self.profile.max_takeoff_mass / self.mass * g
        w = self.wind_est.speed if self.wind_est.converged else 0.0
        drag_acc = self.profile.drag_coefficient() * w * w / self.mass
        horiz = min(g * math.tan(math.radians(self.profile.max_tilt_deg)), math.sqrt(max(t_max**2 - g**2, 0)))
        return float(np.clip(horiz - drag_acc - 1.0, 1.0, 6.0))

    def _replan_around_obstacles(self, pos: np.ndarray) -> None:
        pts = self.world_view.static_points
        pts = pts[np.linalg.norm(pts - pos, axis=1) < 80.0] if len(pts) else pts
        if not len(pts):
            return
        for cluster in _clusters(pts, 4.0):
            self.planner.inflated.add_box(cluster.min(axis=0) - 1.0, cluster.max(axis=0) + 1.0)
        self.status.replans += 1
        goal = self.tracker.goal if self.tracker is not None else self.home
        self.log("perception", f"unmapped obstacle ({len(pts)} voxels) blocks route - replanning")
        self.local.inflated = self.planner.inflated
        self.inflated = self.planner.inflated
        res = self.planner.plan(pos, goal, self.weather_feed.at(self.t).wind_speed, self.cruise_alt)
        if res is not None:
            self.tracker = PathTracker([pos.copy()] + res.waypoints[1:])
            self.log("plan", f"new route {res.length:.0f} m")

    def _approach(self, pos, vel) -> AutonomyOutput:
        hp = self.hover_point
        z_sp = max(hp[2], pos[2] - 2.5)
        sp = vec(hp[0], hp[1], z_sp)
        if norm(pos - hp) < 1.0 and norm(vel) < 0.6:
            self.zone_wait_since = self.t
            self.set_phase(Phase.ZONE_CHECK, "hovering over drop point: checking zone and marker")
        return AutonomyOutput(TrajectorySetpoint(position=sp, velocity=vec(0, 0, -1.5 if pos[2] > hp[2] + 1 else 0.0)))

    def _zone_check(self, f: SensorFrame, pos) -> AutonomyOutput:
        site = self.site
        hold = TrajectorySetpoint(position=self.hover_point.copy(), velocity=np.zeros(3))
        # 1) Authenticate the marker (cryptographic + physical position check).
        if self.marker_verified_for != site.site_id and f.tag is not None and f.tag.payload:
            observed = pos + f.tag.relative
            res = self.verifier.verify(f.tag.payload, self.ticket.order.order_id, site.position, observed, self.t)
            if res.ok:
                self.marker_verified_for = site.site_id
                self.log("security", f"landing marker verified for order {self.ticket.order.order_id}")
            else:
                self.status.security_rejections.append(res.reason)
                self.log("security", f"MARKER REJECTED: {res.reason}")
                return self._abort_delivery(f"security: {res.reason}")
        if self.marker_verified_for != site.site_id:
            if self.t - self.zone_wait_since > 25.0:
                # Fog/glare: creep lower on UWB guidance to read the marker.
                lower = max(site.position[2] + 10.0, self.hover_point[2] - 6.0)
                if self.t - self.zone_wait_since > 60.0:
                    return self._abort_delivery("could not read authentic landing marker")
                return AutonomyOutput(TrajectorySetpoint(position=vec(*self.hover_point[:2], lower), velocity=np.zeros(3)))
            return AutonomyOutput(hold)

        # 2) Zone must be clear (any modality vetoes).
        radius = 4.0 if site.kind == "pad" else 2.5
        clear, reasons = landing_zone_clear(self.world_view, site.position, radius, site.ground_z)
        if not clear:
            if self.zone_blocked_since is None or self.t - self.zone_blocked_since > 20.0:
                if self.zone_attempts >= 3:
                    return self._abort_delivery("drop zone blocked after 3 attempts")
                self.zone_attempts += 1
                self.zone_blocked_since = self.t
                self.log("zone", f"drop zone blocked (attempt {self.zone_attempts}/3): {'; '.join(reasons)}")
            return AutonomyOutput(hold)
        self.zone_blocked_since = None

        if site.kind == "winch":
            drop = self.hover_point[2] - site.position[2]
            self.winch = WinchController(drop, self.payload_kg, self.profile.tether_length_m)
            self.set_phase(Phase.WINCH, f"zone clear, lowering parcel {drop:.0f} m on tether")
        else:
            self.lander = PrecisionLander(self.profile, site.position, start_height=self.hover_point[2] - site.position[2])
            self.set_phase(Phase.DESCENT, "zone clear, precision descent (UWB -> vision)")
        return AutonomyOutput(hold)

    def _abort_delivery(self, why: str) -> AutonomyOutput:
        self.status.abort_reason = why
        self.lander = None
        self.winch = None
        self.set_phase(Phase.CLIMB_OUT, why + " - returning with parcel")
        return AutonomyOutput(TrajectorySetpoint(position=self.nav.position, velocity=vec(0, 0, 2.0)))

    def _land(self, f: SensorFrame, dt: float, nav: NavSolution) -> AutonomyOutput:
        lander = self.lander
        assert lander is not None
        pos = nav.position
        site_landing = self.phase == Phase.DESCENT
        require_tag = lander.require_tag and self.phase in (Phase.DESCENT, Phase.HOME_LANDING)
        zone_ok = True
        if site_landing:
            zone_ok, reasons = landing_zone_clear(self.world_view, lander.pad, 3.0, lander.pad[2])
            if not zone_ok:
                self.log("zone", "descent paused: " + "; ".join(reasons))
        gust = self.wind_est.gust_std if self.wind_est.converged else 0.0
        cmd = lander.update(self.t, pos, self.last_tag_seen, gust, dt, nav_ok=nav.integrity_ok or not require_tag,
                            require_tag=require_tag, zone_clear=zone_ok)
        if cmd.note:
            self.log("landing", f"{cmd.stage.value}: {cmd.note}")
        self.status.go_arounds = max(self.status.go_arounds, lander.go_arounds)
        if cmd.stage == LandingStage.ABORT:
            if self.phase == Phase.DESCENT:
                return self._abort_delivery(cmd.note or "landing aborted")
            lander.go_arounds = 0
            lander.stage = LandingStage.DESCENT  # home/divert: keep trying, there is nowhere better to go
            lander.require_tag = False

        h = pos[2] - lander.pad[2]
        thrust_ratio = norm(f.imu.thrust_accel) / 9.81
        votes = touchdown_votes(f.rangefinder, h, nav.velocity[2], thrust_ratio)
        if lander.stage in (LandingStage.FLARE, LandingStage.TERMINAL) and votes.consensus:
            lander.touched_down()
            err = horizontal_distance(pos, lander.pad)
            self.log("landing", f"touchdown confirmed by {votes.count}/4 independent cues, error {err:.2f} m")
            if self.phase == Phase.DESCENT:
                self.status.delivery_error_m = err
                self.release_wait_since = self.t
                self.set_phase(Phase.LANDED_AT_SITE)
            elif self.phase == Phase.HOME_LANDING:
                self.fc.disarm()
                self.set_phase(Phase.COMPLETE, "landed at base")
            else:
                self.fc.disarm()
                self.set_phase(Phase.LANDED_ELSEWHERE, "landed at contingency site")
            return AutonomyOutput(TrajectorySetpoint(velocity=vec(0, 0, -0.5)))
        if f.rangefinder is not None and f.rangefinder.distance < 0.35 and not votes.consensus and h > 1.0:
            self.log("perception", f"rangefinder reports ground at {f.rangefinder.distance:.2f} m but only "
                                   f"{votes.count}/4 cues agree - ignoring (rain/clutter)")
        return AutonomyOutput(TrajectorySetpoint(position=cmd.position, velocity=cmd.velocity))

    def _authorised(self) -> bool:
        order = self.ticket.order
        if not order.require_otp:
            return True
        code = self.recipient_code_fn(self.t) if self.recipient_code_fn else None
        if code is None:
            return False
        ok = verify_one_time_code(self.ticket.otp_secret, self.ticket.otp_counter, code)
        if not ok:
            self.log("security", "recipient code rejected")
        return ok

    def _release_on_pad(self, pos) -> AutonomyOutput:
        sp = TrajectorySetpoint(velocity=vec(0, 0, -0.5))
        if self._authorised():
            self._mark_delivered("landed")
            self.set_phase(Phase.CLIMB_OUT, "parcel released, recipient verified")
            return AutonomyOutput(sp, release_payload=True)
        if self.t - self.release_wait_since > 90.0:
            return self._abort_delivery("recipient did not authorise release")
        self.log("security", "waiting for recipient one-time code")
        return AutonomyOutput(sp)

    def _winch(self, f: SensorFrame, dt: float, pos) -> AutonomyOutput:
        w = self.winch
        sway = f.winch_sway if f.winch_sway is not None else np.zeros(3)
        sway_rate = f.winch_sway_rate if f.winch_sway_rate is not None else np.zeros(3)
        authorised = w.stage != WinchStage.ON_GROUND or self._authorised()
        cmd = w.update(self.t, f.winch_payout_m or 0.0, f.winch_tension_n or 0.0, sway, sway_rate, dt, authorised)
        if cmd.note:
            self.log("winch", cmd.note)
        if w.stage == WinchStage.ON_GROUND and self.release_wait_since is None:
            self.release_wait_since = self.t
        if w.stage == WinchStage.ON_GROUND and self.t - (self.release_wait_since or self.t) > 90.0:
            self.log("winch", "no recipient authorisation: reeling parcel back in")
            self.status.abort_reason = "recipient did not authorise release"
            w.abort()
        release = cmd.release
        if release:
            self._mark_delivered("winch")
        if cmd.cut:
            self.status.abort_reason = cmd.note
            self.payload_kg = 0.0
            self.mass = self.profile.empty_mass_kg
        if w.stage in (WinchStage.DONE, WinchStage.SNAG_CUT):
            if not self.status.delivered and self.status.abort_reason is None:
                self.status.abort_reason = "parcel not delivered"
            self.set_phase(Phase.CLIMB_OUT, "winch stowed" if w.stage == WinchStage.DONE else "tether cut")
        sp = TrajectorySetpoint(position=self.hover_point + cmd.drone_offset, velocity=np.zeros(3))
        return AutonomyOutput(sp, winch=cmd, release_payload=release)

    def _mark_delivered(self, method: str) -> None:
        self.status.delivered = True
        self.status.delivery_method = method
        self.payload_kg = 0.0
        self.mass = self.profile.empty_mass_kg
        self.log("delivery", f"parcel delivered by {method} for order {self.ticket.order.order_id}")


def _clusters(pts: np.ndarray, radius: float) -> list[np.ndarray]:
    """Single-linkage clustering of obstacle voxels (small N)."""
    n = len(pts)
    label = -np.ones(n, dtype=int)
    cur = 0
    for i in range(n):
        if label[i] >= 0:
            continue
        stack = [i]
        label[i] = cur
        while stack:
            j = stack.pop()
            near = np.where((label < 0) & (np.linalg.norm(pts - pts[j], axis=1) < radius))[0]
            label[near] = cur
            stack.extend(near.tolist())
        cur += 1
    return [pts[label == k] for k in range(cur)]
