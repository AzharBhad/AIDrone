"""Physics-flavoured sensor models with weather and urban-canyon degradation."""

from __future__ import annotations

import math

import numpy as np

from ..config import VehicleProfile
from ..geometry import norm
from ..measurements import (
    BaroMeasurement,
    CameraDetection,
    GnssFix,
    ImuSample,
    RadarTrack,
    RangefinderMeasurement,
    SensorFrame,
    TagDetection,
    UwbRanges,
    VioMeasurement,
)
from .world import World

RATES = {"gnss": 5.0, "vio": 20.0, "baro": 20.0, "uwb": 10.0, "tag": 10.0, "lidar": 10.0, "radar": 10.0,
         "camera": 5.0, "range": 20.0}


class SensorSimulator:
    def __init__(self, world: World, profile: VehicleProfile, seed: int = 0) -> None:
        self.world = world
        self.profile = profile
        self.rng = np.random.default_rng(seed + 1000)
        self.next_t = {k: 0.0 for k in RATES}
        n = 24
        az = self.rng.uniform(0, 2 * math.pi, n)
        el = np.radians(self.rng.uniform(12, 85, n))
        self.sat_dirs = np.stack([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], axis=1)
        self.multipath = np.zeros(3)
        self.baro_bias = 0.0
        self.vio_bias = np.zeros(3)
        el_l = np.radians([-45, -30, -20, -10, -4, 0, 5, 15])
        az_l = np.radians(np.arange(0, 360, 7.5))
        E, A = np.meshgrid(el_l, az_l)
        self.lidar_dirs = np.stack([np.cos(E) * np.cos(A), np.cos(E) * np.sin(A), np.sin(E)], axis=-1).reshape(-1, 3)
        self.last_gnss_frac = 1.0

    def _due(self, name: str, t: float) -> bool:
        if t + 1e-9 >= self.next_t[name]:
            self.next_t[name] = t + 1.0 / RATES[name]
            return True
        return False

    # ------------------------------------------------------------------ main
    def frame(self, t: float, dyn, winch_tension: float | None = None, winch_payout: float | None = None) -> SensorFrame:
        w = self.world.weather(t)
        p, v = dyn.p, dyn.v
        rng = self.rng
        city = self.world.city
        f = SensorFrame(
            t=t,
            imu=ImuSample(dyn.a + rng.normal(0, 0.15, 3), dyn.thrust_acc + rng.normal(0, 0.2, 3)),
            winch_tension_n=winch_tension,
            winch_payout_m=winch_payout,
            link_ok=self.world.link_ok(t),
        )
        dynamic = self.world.dynamic_spheres(t)
        if self._due("gnss", t):
            f.gnss = self._gnss(p, 1.0 / RATES["gnss"])
        if self._due("baro", t):
            self.baro_bias += rng.normal(0, 0.01)
            gust_err = 0.05 * w.gust_std * rng.standard_normal()
            f.baro = BaroMeasurement(p[2] + self.baro_bias + gust_err + rng.normal(0, 0.25), 0.4)
        if self._due("vio", t):
            f.vio = self._vio(p, v, w)
        if self._due("range", t):
            g = city.ground_level(p[0], p[1])
            d = p[2] - g
            if d < 40.0:
                false_drop = rng.random() < 0.3 * min(w.rain_mm_h / 50.0, 1.0) + 0.2 * min(w.snow_mm_h / 10.0, 1.0)
                f.rangefinder = RangefinderMeasurement(rng.uniform(0.1, 0.4) if false_drop else max(0.0, d + rng.normal(0, 0.02)))
        if self._due("uwb", t):
            f.uwb = self._uwb(p)
        if self._due("tag", t):
            f.tag = self._tag(p, w)
        if self._due("lidar", t):
            f.lidar_points = self._lidar(p, w, dynamic)
        if self._due("radar", t) and self.profile.has_radar:
            f.radar = self._radar(p, t)
        if self._due("camera", t):
            f.camera = self._camera(p, t, w)
        return f

    # ------------------------------------------------------------------ GNSS
    def _gnss(self, p: np.ndarray, dt: float) -> GnssFix:
        rng = self.rng
        rng_hits = self.world.city.raycast(p + np.array([0, 0, 0.3]), self.sat_dirs, 250.0, step=3.0)
        frac = float(np.mean(np.isinf(rng_hits)))
        self.last_gnss_frac = frac
        # Multipath bias wanders when the sky is blocked, decays in the open.
        self.multipath = 0.9 * self.multipath + (1 - frac) * rng.normal(0, 1.5, 3) * math.sqrt(dt) * 3
        self.multipath[2] *= 0.5
        sats = int(round(frac * 32))
        hdop = 0.7 / math.sqrt(max(frac, 0.05))
        if frac >= 0.75 and self.world.rtk_available:
            return GnssFix(p + rng.normal(0, [0.02, 0.02, 0.04]), 0.03, 0.05, "rtk_fixed", sats, hdop)
        if frac >= 0.55 and self.world.rtk_available:
            return GnssFix(p + rng.normal(0, 0.35, 3) + 0.3 * self.multipath, 0.4, 0.6, "rtk_float", sats, hdop)
        if frac >= 0.3 or (frac >= 0.2 and not self.world.rtk_available):
            meas = p + rng.normal(0, 2.0, 3) + self.multipath
            if rng.random() < 0.08:  # NLOS reflection: a big, confident-looking jump
                ang = rng.uniform(0, 2 * math.pi)
                meas = meas + rng.uniform(15, 40) * np.array([math.cos(ang), math.sin(ang), 0.0])
            return GnssFix(meas, 2.5, 4.0, "3d", sats, hdop)
        return GnssFix(p, 99.0, 99.0, "none", sats, hdop)

    # ------------------------------------------------------------------- VIO
    def _vio(self, p, v, w) -> VioMeasurement | None:
        city = self.world.city
        agl = p[2] - city.ground_level(p[0], p[1])
        light = min(1.0, w.illumination_lux / 30.0)
        if self.profile.has_nir_illuminator and agl < 40.0:
            light = max(light, 0.75)
        vis = min(1.0, w.visibility_m / 150.0)
        lens = 1.0 - 0.25 * min(w.rain_mm_h / 50.0, 1.0) - 0.3 * min(w.snow_mm_h / 10.0, 1.0)
        height = 0.8 if agl > 100 else 1.0
        q = float(np.clip(light * vis * lens * height, 0.0, 1.0))
        if q < 0.25:
            return None
        self.vio_bias = 0.995 * self.vio_bias + self.rng.normal(0, 0.003, 3)
        sigma = 0.06 / q
        return VioMeasurement(v + self.vio_bias + self.rng.normal(0, sigma, 3), sigma * 1.3, q)

    # ------------------------------------------------------------------- UWB
    def _uwb(self, p) -> UwbRanges | None:
        city = self.world.city
        for site in list(city.sites.values()) + [city.depot]:
            if site is None or norm(site.position - p) > 90.0:
                continue
            anchors = np.array(site.anchors)
            ranges = []
            for a in anchors:
                r = norm(p - a) + self.rng.normal(0, 0.08)
                if not city.line_of_sight(a + np.array([0, 0, 0.2]), p, step=1.0):
                    r += self.rng.uniform(0.5, 2.5)  # NLOS: always longer, never shorter
                ranges.append(r)
            return UwbRanges(anchors, np.array(ranges), 0.12)
        return None

    # ------------------------------------------------------------- fiducials
    def _tag(self, p, w) -> TagDetection | None:
        city = self.world.city
        for site in list(city.sites.values()) + [city.depot]:
            if site is None:
                continue
            marker = self.world.marker_position(site.site_id)
            rel = marker - p
            dist = norm(rel)
            if dist > 60 or dist < 0.3 or rel[2] > 0:
                continue
            off_nadir = math.atan2(math.hypot(rel[0], rel[1]), -rel[2])
            if off_nadir > math.radians(40):
                continue
            light = min(1.0, w.illumination_lux / 30.0)
            if self.profile.has_nir_illuminator and dist < 30:
                light = max(light, 0.9)
            vis = min(1.0, w.visibility_m / 80.0)
            rain = 1.0 - 0.3 * min(w.rain_mm_h / 50.0, 1.0)
            # Nested tags: large outer for altitude, small inner for the last metres.
            max_range = 45.0 * site.tag_size_m * light * vis * rain
            if dist > max_range or self.rng.random() > 0.92:
                continue
            payload = self.world.marker_payloads.get(site.site_id, b"")
            sigma = 0.004 * dist + 0.01
            return TagDetection(rel + self.rng.normal(0, sigma, 3), sigma * 1.5, payload)
        return None

    # ----------------------------------------------------------------- LiDAR
    def _lidar(self, p, w, dynamic) -> np.ndarray:
        max_r = 80.0 * min(1.0, w.visibility_m / 400.0) ** 0.5 * (1.0 - 0.4 * min(w.rain_mm_h / 50.0, 1.0))
        max_r = max(max_r, 12.0)
        r = self.world.city.raycast(p, self.lidar_dirs, max_r, step=1.0, include_unmapped=True, dynamic=dynamic)
        ok = np.isfinite(r)
        pts = self.lidar_dirs[ok] * (r[ok] + self.rng.normal(0, 0.05, ok.sum()))[:, None]
        n_clutter = self.rng.poisson(0.8 * w.rain_mm_h + 3.0 * w.snow_mm_h + (30 if w.visibility_m < 200 else 0))
        if n_clutter:
            d = self.rng.normal(size=(n_clutter, 3))
            d /= np.linalg.norm(d, axis=1, keepdims=True)
            pts = np.vstack([pts, d * self.rng.uniform(1.5, 8.0, (n_clutter, 1))])
        return pts

    # ----------------------------------------------------------------- radar
    def _radar(self, p, t) -> list[RadarTrack]:
        out = []
        for m in self.world.movers:
            q = m.position(t)
            if q is None or norm(q - p) > 150 or self.rng.random() > 0.95:
                continue
            out.append(RadarTrack(q - p + self.rng.normal(0, 0.5, 3), m.velocity + self.rng.normal(0, 0.3, 3), m.radius))
        for ob in self.world.city.obstacles:
            nearest = np.clip(p, ob.lo, ob.hi)
            if norm(nearest - p) < 120 and self.rng.random() < 0.9:
                out.append(RadarTrack(nearest - p + self.rng.normal(0, 0.5, 3), np.zeros(3), 1.5))
        for person in self.world.people:
            if person.present(t) and norm(person.position - p) < 30 and self.rng.random() < 0.4:
                out.append(RadarTrack(person.position - p + self.rng.normal(0, 0.5, 3), np.zeros(3), 0.5))
        return out

    # ---------------------------------------------------------------- camera
    def _camera(self, p, t, w) -> list[CameraDetection]:
        vis = min(1.0, w.visibility_m / 200.0)
        light = min(1.0, w.illumination_lux / 50.0)
        out = []
        for person in self.world.people:
            if not person.present(t):
                continue
            d = norm(person.position - p)
            if d > 50:
                continue
            prob = 0.95 * vis * max(light, 0.9 if self.profile.has_thermal_camera else 0.0)
            if self.rng.random() < prob:
                out.append(CameraDetection(person.position - p + self.rng.normal(0, 0.4, 3), "person", float(0.6 + 0.4 * prob)))
        for m in self.world.movers:
            q = m.position(t)
            if q is None or norm(q - p) > 80:
                continue
            prob = 0.9 * vis * max(light, 0.5)
            if self.rng.random() < prob:
                out.append(CameraDetection(q - p + self.rng.normal(0, 0.6, 3), m.label, float(prob)))
        return out
