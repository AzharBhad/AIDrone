"""Companion-computer <-> flight-controller boundary (doc 1.2 "split brain").

The autonomy stack never touches motors. It streams
:class:`TrajectorySetpoint` messages (PX4 ``px4_msgs/TrajectorySetpoint``
semantics: NED frame, NaN = "do not control this axis") at >= 2 Hz. The
flight controller owns the 800 Hz rate / 250 Hz attitude / 100 Hz position
loops and its own failsafe: if the offboard heartbeat stops for longer than
``offboard_timeout`` it holds position, then lands - so a frozen companion
computer can never fly the aircraft into a building.

:class:`SimFlightController` implements that contract over the simulated
airframe. :class:`Px4Ros2Bridge` is the same interface over ROS 2 /
uXRCE-DDS for the real aircraft and is only importable where ``rclpy`` and
``px4_msgs`` exist.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from .geometry import enu_to_ned


@dataclass
class TrajectorySetpoint:
    """ENU setpoint; ``None`` axes are left to the flight controller."""

    position: np.ndarray | None = None
    velocity: np.ndarray | None = None
    acceleration: np.ndarray | None = None
    yaw: float | None = None

    def to_px4(self, timestamp_us: int = 0) -> dict:
        """Field layout of px4_msgs/msg/TrajectorySetpoint (NED, NaN = unset)."""
        nan3 = [math.nan] * 3

        def conv(v):
            return enu_to_ned(v).tolist() if v is not None else nan3

        yaw = math.nan
        if self.yaw is not None:
            yaw = math.pi / 2 - self.yaw  # ENU yaw (from East, CCW) -> NED heading (from North, CW)
        return {
            "timestamp": timestamp_us,
            "position": conv(self.position),
            "velocity": conv(self.velocity),
            "acceleration": conv(self.acceleration),
            "jerk": nan3,
            "yaw": yaw,
            "yawspeed": math.nan,
        }


class FlightControllerInterface(ABC):
    offboard_timeout: float = 0.5

    @abstractmethod
    def arm(self) -> bool: ...

    @abstractmethod
    def disarm(self) -> None: ...

    @abstractmethod
    def send_setpoint(self, sp: TrajectorySetpoint, t: float) -> None: ...

    @abstractmethod
    def set_wind_feedforward(self, wind: np.ndarray) -> None: ...

    @abstractmethod
    def deploy_parachute(self) -> None: ...


class SimFlightController(FlightControllerInterface):
    def __init__(self, dynamics) -> None:
        self.dyn = dynamics
        self.armed = False
        self.last_sp: TrajectorySetpoint | None = None
        self.last_sp_t = -math.inf
        self.failsafe: str | None = None
        self.hold_position: np.ndarray | None = None

    def arm(self) -> bool:
        self.armed = True
        self.dyn.armed = True
        return True

    def disarm(self) -> None:
        self.armed = False
        self.dyn.armed = False

    def send_setpoint(self, sp: TrajectorySetpoint, t: float) -> None:
        self.last_sp = sp
        self.last_sp_t = t
        if self.failsafe == "offboard_lost":
            self.failsafe = None
            self.hold_position = None

    def set_wind_feedforward(self, wind: np.ndarray) -> None:
        self.dyn.wind_ff = np.asarray(wind, dtype=float)

    def deploy_parachute(self) -> None:
        self.dyn.parachute = True

    def step(self, t: float, dt: float, wind: np.ndarray) -> None:
        sp = self.last_sp
        if self.armed and t - self.last_sp_t > self.offboard_timeout:
            if self.failsafe != "offboard_lost":
                self.failsafe = "offboard_lost"
                self.hold_position = self.dyn.p.copy()
                self.failsafe_t = t
            if t - self.failsafe_t < 10.0:
                sp = TrajectorySetpoint(position=self.hold_position, velocity=np.zeros(3))
            else:  # still nothing from the companion computer: land in place
                sp = TrajectorySetpoint(velocity=np.array([0.0, 0.0, -1.0]))
        self.dyn.step(dt, sp if self.armed else None, wind)


class Px4Ros2Bridge(FlightControllerInterface):  # pragma: no cover - needs a ROS 2 install
    """Publishes setpoints to PX4 over uXRCE-DDS. Requires rclpy + px4_msgs."""

    def __init__(self, node) -> None:
        from px4_msgs.msg import OffboardControlMode, TrajectorySetpoint as Px4Setpoint, VehicleCommand  # noqa: F401

        self.node = node
        self._Px4Setpoint = Px4Setpoint
        self._Mode = OffboardControlMode
        self._Cmd = VehicleCommand
        self.sp_pub = node.create_publisher(Px4Setpoint, "/fmu/in/trajectory_setpoint", 10)
        self.mode_pub = node.create_publisher(OffboardControlMode, "/fmu/in/offboard_control_mode", 10)
        self.cmd_pub = node.create_publisher(VehicleCommand, "/fmu/in/vehicle_command", 10)

    def _now_us(self) -> int:
        return self.node.get_clock().now().nanoseconds // 1000

    def _command(self, command: int, **params) -> None:
        msg = self._Cmd()
        msg.command = command
        for k, v in params.items():
            setattr(msg, k, float(v))
        msg.target_system = 1
        msg.target_component = 1
        msg.source_system = 1
        msg.source_component = 1
        msg.from_external = True
        msg.timestamp = self._now_us()
        self.cmd_pub.publish(msg)

    def arm(self) -> bool:
        self._command(self._Cmd.VEHICLE_CMD_DO_SET_MODE, param1=1.0, param2=6.0)  # offboard
        self._command(self._Cmd.VEHICLE_CMD_COMPONENT_ARM_DISARM, param1=1.0)
        return True

    def disarm(self) -> None:
        self._command(self._Cmd.VEHICLE_CMD_COMPONENT_ARM_DISARM, param1=0.0)

    def send_setpoint(self, sp: TrajectorySetpoint, t: float) -> None:
        ts = self._now_us()
        mode = self._Mode()
        mode.timestamp = ts
        mode.position = sp.position is not None
        mode.velocity = sp.velocity is not None
        mode.acceleration = sp.acceleration is not None
        self.mode_pub.publish(mode)
        d = sp.to_px4(ts)
        msg = self._Px4Setpoint()
        msg.timestamp = ts
        msg.position = d["position"]
        msg.velocity = d["velocity"]
        msg.acceleration = d["acceleration"]
        msg.yaw = d["yaw"]
        self.sp_pub.publish(msg)

    def set_wind_feedforward(self, wind: np.ndarray) -> None:
        # PX4 estimates wind internally (EKF2 wind states); nothing to send.
        pass

    def deploy_parachute(self) -> None:
        self._command(self._Cmd.VEHICLE_CMD_DO_PARACHUTE, param1=2.0)  # PARACHUTE_RELEASE
