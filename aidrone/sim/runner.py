"""Closed-loop simulation: world -> sensors -> autonomy -> flight controller -> physics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..autonomy import TERMINAL_PHASES, AutonomyCore, Event, Phase
from ..citymap import CityMap
from ..config import VehicleProfile
from ..fleet import DeliveryTicket
from ..flight_controller import SimFlightController
from ..geometry import G_VEC, norm
from ..safety import Battery, EnergyModel, VehicleHealth
from ..security import MarkerVerifier
from .dynamics import MultirotorDynamics
from .sensors import SensorSimulator
from .world import World


class TetherSim:
    """Parcel on a winch line: pendulum with wind drag, ground contact, tension."""

    def __init__(self, mass: float, drone_pos: np.ndarray) -> None:
        self.m = mass
        self.payout = 0.0
        self.p = drone_pos - np.array([0, 0, 0.5])
        self.v = np.zeros(3)
        self.attached = True
        self.on_ground = False
        self.tension = mass * 9.81
        self.cut = False

    def step(self, dt, drone_p, drone_v, rate, wind, ground_z) -> None:
        if not self.attached:
            self.payout = max(0.0, self.payout + min(rate, 0.0) * dt)
            self.tension = 0.0
            return
        self.payout = float(np.clip(self.payout + rate * dt, 0.0, 40.0))
        v_air = self.v - wind
        drag = -0.5 * 1.225 * 0.08 * norm(v_air) * v_air
        self.v = self.v + (G_VEC + drag / self.m) * dt
        self.p = self.p + self.v * dt
        if self.p[2] <= ground_z:
            self.p[2] = ground_z
            self.v[:] = 0.0
            self.on_ground = True
        else:
            self.on_ground = False
        r = self.p - drone_p
        d = norm(r)
        L = self.payout + 0.5
        if d > L:
            u = r / d
            self.p = drone_p + u * L
            rel = self.v - drone_v
            radial = float(rel @ u)
            if radial > 0:
                self.v = self.v - radial * u
            tangential = rel - float(rel @ u) * u
            self.tension = max(0.0, self.m * (9.81 * -u[2] + norm(tangential) ** 2 / L))
        else:
            self.tension = 0.0


@dataclass
class SimResult:
    scenario: str
    final_phase: str
    delivered: bool
    delivery_method: str | None
    delivery_error_m: float | None
    parcel_error_m: float | None
    abort_reason: str | None
    flight_time_s: float
    energy_used_wh: float
    min_clearance_m: float
    collided: bool
    max_impact_speed: float
    replans: int
    go_arounds: int
    security_rejections: list[str]
    events: list[Event] = field(default_factory=list)
    track: np.ndarray | None = None

    @property
    def safe_outcome(self) -> bool:
        return not self.collided and self.max_impact_speed < 3.0

    def summary(self) -> str:
        lines = [
            f"scenario         : {self.scenario}",
            f"final phase      : {self.final_phase}",
            f"delivered        : {self.delivered}" + (f" ({self.delivery_method})" if self.delivered else ""),
        ]
        if self.delivery_error_m is not None:
            lines.append(f"landing error    : {self.delivery_error_m * 100:.1f} cm")
        if self.parcel_error_m is not None:
            lines.append(f"parcel placement : {self.parcel_error_m * 100:.1f} cm from drop point")
        if self.abort_reason:
            lines.append(f"abort reason     : {self.abort_reason}")
        lines += [
            f"flight time      : {self.flight_time_s:.0f} s",
            f"energy used      : {self.energy_used_wh:.0f} Wh",
            f"min clearance    : {self.min_clearance_m:.1f} m",
            f"collision        : {self.collided}",
            f"replans          : {self.replans}, go-arounds: {self.go_arounds}",
        ]
        if self.security_rejections:
            lines.append(f"security         : {'; '.join(self.security_rejections)}")
        return "\n".join(lines)


class Simulation:
    def __init__(
        self,
        world: World,
        hd_map: CityMap,
        profile: VehicleProfile,
        ticket: DeliveryTicket,
        verifier: MarkerVerifier,
        name: str = "custom",
        recipient_code_fn=None,
        seed: int = 0,
        dt: float = 0.05,
        substeps: int = 4,
        t_max: float = 1500.0,
        start_soc: float = 1.0,
        forecast=None,
    ) -> None:
        self.world = world
        self.profile = profile
        self.name = name
        self.dt = dt
        self.substeps = substeps
        self.t_max = t_max
        city = world.city
        mass = profile.empty_mass_kg + ticket.order.payload_kg
        self.dyn = MultirotorDynamics(profile, city.depot.position, mass, ground_fn=city.ground_level, seed=seed)
        self.fc = SimFlightController(self.dyn)
        self.sensors = SensorSimulator(world, profile, seed)
        self.battery = Battery(profile.battery_wh, profile.battery_wh * start_soc)
        self.energy = EnergyModel(profile)
        self.core = AutonomyCore(profile, hd_map, verifier, self.fc, self.battery, world.timeline, forecast)
        self.ticket = ticket
        self.recipient_code_fn = recipient_code_fn
        self.tether: TetherSim | None = None
        self.parcel_rest: np.ndarray | None = None
        self.min_clearance = math.inf

    def run(self) -> SimResult:
        core, dyn, world = self.core, self.dyn, self.world
        t = 0.0
        f0 = self.sensors.frame(0.0, dyn)
        core.preflight(self.ticket, 0.0, f0.baro.altitude if f0.baro else None, self.recipient_code_fn)
        start_energy = self.battery.energy_wh
        track = []
        failures = sorted(world.motor_failures)
        airborne_time = 0.0
        commanded_descent = 0.0
        while t < self.t_max and core.phase not in TERMINAL_PHASES:
            # Faults injected by the scenario.
            while failures and t >= failures[0]:
                dyn.fail_motor()
                failures.pop(0)
            frame = self.sensors.frame(t, dyn, *self._winch_readings())
            if self.tether is not None:
                frame.winch_sway = self.tether.p - dyn.p
                frame.winch_sway[2] = 0.0
                frame.winch_sway_rate = self.tether.v - dyn.v
                frame.winch_sway_rate[2] = 0.0
            health = VehicleHealth(dyn.motors_ok, self.profile.motor_count, dyn.thrust_to_weight, 0.0,
                                   -dyn.v[2], commanded_descent)
            out = core.tick(frame, self.dt, health)
            sp_v = out.setpoint.velocity
            commanded_descent = max(0.0, -float(sp_v[2])) + 1.0 if sp_v is not None else 3.0
            self.fc.send_setpoint(out.setpoint, t)
            if out.parachute:
                self.fc.deploy_parachute()
            self._handle_payload(out, core)
            if core.phase in TERMINAL_PHASES and core.phase != Phase.PARACHUTE:
                break
            h = self.dt / self.substeps
            for _ in range(self.substeps):
                world.wind.step(t, h)
                wind = world.wind.wind_at(dyn.p, t)
                self.fc.step(t, h, wind)
                if self.tether is not None:
                    rate = out.winch.payout_rate if out.winch else 0.0
                    self.tether.step(h, dyn.p, dyn.v, rate, wind, world.city.ground_level(self.tether.p[0], self.tether.p[1]))
                t += h
            if not dyn.on_ground:
                airborne_time += self.dt
                heaters = bool(core.weather and core.weather.heaters_on)
                v_air = norm(dyn.v - world.wind.wind_at(dyn.p, t))
                self.battery.draw(self.energy.power(dyn.mass, v_air, world.weather(t).gust_std, heaters), self.dt)
            self._check_collision(t)
            track.append(dyn.p.copy())
            if dyn.collided:
                core.log("crash", "collision with structure")
                break
            if core.phase == Phase.PARACHUTE and dyn.on_ground:
                break
        st = core.status
        parcel_err = None
        if self.parcel_rest is not None and core.site is not None:
            parcel_err = float(np.hypot(*(self.parcel_rest[:2] - core.site.position[:2])))
        return SimResult(
            scenario=self.name,
            final_phase=core.phase.value,
            delivered=st.delivered,
            delivery_method=st.delivery_method,
            delivery_error_m=st.delivery_error_m,
            parcel_error_m=parcel_err,
            abort_reason=st.abort_reason,
            flight_time_s=airborne_time,
            energy_used_wh=start_energy - self.battery.energy_wh,
            min_clearance_m=self.min_clearance,
            collided=dyn.collided,
            max_impact_speed=dyn.max_impact_speed,
            replans=st.replans,
            go_arounds=st.go_arounds,
            security_rejections=st.security_rejections,
            events=core.events,
            track=np.array(track) if track else None,
        )

    def _winch_readings(self):
        if self.tether is None:
            return None, None
        return self.tether.tension, self.tether.payout

    def _handle_payload(self, out, core: AutonomyCore) -> None:
        if core.phase == Phase.WINCH and self.tether is None:
            self.tether = TetherSim(self.ticket.order.payload_kg, self.dyn.p.copy())
        if out.winch is not None and self.tether is not None:
            if out.winch.cut:
                self.tether.attached = False
                self.tether.cut = True
                self.dyn.mass = self.profile.empty_mass_kg
        if out.release_payload:
            self.dyn.mass = self.profile.empty_mass_kg
            if self.tether is not None:
                self.tether.attached = False
                self.parcel_rest = self.tether.p.copy()
            else:
                self.parcel_rest = self.dyn.p.copy()
        if self.tether is not None and self.tether.payout <= 0.0 and core.phase != Phase.WINCH:
            self.tether = None

    def _check_collision(self, t: float) -> None:
        city = self.world.city
        p = self.dyn.p
        if self.dyn.on_ground:
            return
        # Clearance to structures (sampled ring) and to moving traffic.
        r = self.profile.safety_radius_m
        dirs = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0.7, 0.7, 0], [-0.7, 0.7, 0], [0.7, -0.7, 0], [-0.7, -0.7, 0]])
        ranges = city.raycast(p, dirs, 15.0, step=0.5, dynamic=self.world.dynamic_spheres(t))
        near = float(np.min(ranges)) if np.isfinite(ranges).any() else 15.0
        self.min_clearance = min(self.min_clearance, near)
        if near < r * 0.8 or city.occupied(p[None, :], dynamic=self.world.dynamic_spheres(t))[0]:
            self.dyn.collided = True
