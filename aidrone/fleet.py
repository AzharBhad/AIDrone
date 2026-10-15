"""Dispatch side: orders, signed delivery tickets, fleet-level go/no-go.

This is the ground-system half of the split architecture (doc 1.2): it is
not in the real-time loop, it issues missions, signs landing markers and
decides whether a launch window exists given the forecast.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .citymap import CityMap
from .config import VehicleProfile
from .security import MarkerClaims, MarkerSigner, new_nonce, one_time_code
from .weather import OpsLevel, WeatherTimeline, assess_weather


@dataclass
class DeliveryOrder:
    order_id: str
    site_id: str
    payload_kg: float
    require_otp: bool = True


@dataclass
class DeliveryTicket:
    order: DeliveryOrder
    marker_payload: bytes  # printed / displayed at the drop point
    otp_secret: bytes  # shared with the recipient app only
    otp_counter: int = 0

    def recipient_code(self) -> str:
        return one_time_code(self.otp_secret, self.otp_counter)


class Dispatcher:
    def __init__(self, city: CityMap, signer: MarkerSigner | None = None) -> None:
        self.city = city
        self.signer = signer or MarkerSigner()

    @property
    def public_key(self):
        return self.signer.public_key

    def create_ticket(self, order: DeliveryOrder, now: float, ttl_s: float = 3600.0) -> DeliveryTicket:
        site = self.city.sites[order.site_id]
        claims = MarkerClaims(
            order_id=order.order_id,
            site_id=site.site_id,
            position=tuple(float(x) for x in site.position),
            expires_at=now + ttl_s,
            nonce=new_nonce(),
        )
        return DeliveryTicket(order, self.signer.sign(claims), os.urandom(20))

    def launch_window(
        self,
        profile: VehicleProfile,
        forecast: WeatherTimeline,
        now: float,
        mission_s: float = 1200.0,
    ) -> tuple[bool, list[str]]:
        """Fleet-level check over the whole expected mission duration."""
        reasons: list[str] = []
        ok = True
        for w in forecast.worst_between(now, now + mission_s, step=60.0):
            a = assess_weather(w, profile, airborne=False)
            if a.level >= OpsLevel.HOLD:
                ok = False
                reasons.extend(r for r in a.reasons if r not in reasons)
        return ok, reasons
