import math

import numpy as np

from aidrone.config import URBAN_COURIER
from aidrone.weather import OpsLevel, WeatherState, WeatherTimeline, WindEstimator, assess_weather
from aidrone.geometry import G_VEC


def test_clear_weather_is_nominal():
    a = assess_weather(WeatherState(wind_speed=3, gust_std=0.5), URBAN_COURIER)
    assert a.level == OpsLevel.NOMINAL and a.go and a.speed_factor == 1.0


def test_strong_wind_degrades_then_holds_preflight_and_lands_airborne():
    breezy = assess_weather(WeatherState(wind_speed=10, gust_std=1.0), URBAN_COURIER)
    assert breezy.level == OpsLevel.CAUTION and breezy.speed_factor < 1.0
    deg = assess_weather(WeatherState(wind_speed=12.5, gust_std=1.0), URBAN_COURIER)
    assert deg.level == OpsLevel.DEGRADED and deg.go
    assert deg.speed_factor < 1.0 and deg.altitude_cap_agl is not None
    pre = assess_weather(WeatherState(wind_speed=22, gust_std=4.0), URBAN_COURIER, airborne=False)
    assert pre.level == OpsLevel.HOLD and not pre.go
    air = assess_weather(WeatherState(wind_speed=22, gust_std=4.0), URBAN_COURIER, airborne=True)
    assert air.level == OpsLevel.LAND_NOW


def test_measured_wind_overrides_benign_forecast():
    a = assess_weather(WeatherState(wind_speed=2), URBAN_COURIER, airborne=True, measured_wind=17.0)
    assert a.level >= OpsLevel.RETURN


def test_lightning_rules():
    near = assess_weather(WeatherState(lightning_km=5), URBAN_COURIER, airborne=True)
    assert near.level == OpsLevel.LAND_NOW
    standoff = assess_weather(WeatherState(lightning_km=12), URBAN_COURIER, airborne=True)
    assert standoff.level == OpsLevel.RETURN
    far = assess_weather(WeatherState(lightning_km=40), URBAN_COURIER, airborne=True)
    assert far.level == OpsLevel.NOMINAL


def test_icing_heaters_vs_freezing_rain():
    snow = assess_weather(WeatherState(temperature_c=-5, snow_mm_h=3, visibility_m=2000), URBAN_COURIER)
    assert snow.go and snow.heaters_on
    fz = assess_weather(WeatherState(temperature_c=-1, rain_mm_h=2, freezing_rain=True), URBAN_COURIER)
    assert not fz.go


def test_fog_derates_vision_but_not_radar():
    a = assess_weather(WeatherState(visibility_m=180, illumination_lux=0.5), URBAN_COURIER)
    assert a.go and a.level == OpsLevel.DEGRADED
    assert a.sensor_weights["camera"] < 0.3 and a.sensor_weights["radar"] == 1.0
    assert a.sensor_weights["lidar"] < 1.0


def test_heavy_rain_derates_lidar():
    a = assess_weather(WeatherState(rain_mm_h=35), URBAN_COURIER)
    assert a.go and a.sensor_weights["lidar"] < 0.7


def test_timeline_interpolates_and_handles_no_lightning():
    tl = WeatherTimeline([(0, WeatherState(wind_speed=0, wind_from_deg=350)),
                          (100, WeatherState(wind_speed=10, wind_from_deg=10, lightning_km=5))])
    mid = tl.at(50)
    assert math.isclose(mid.wind_speed, 5.0)
    assert math.isclose(mid.wind_from_deg, 0.0, abs_tol=1e-6)  # wraps through north
    assert mid.lightning_km < 200
    assert math.isinf(tl.at(0).lightning_km)
    assert tl.at(500).wind_speed == 10


def test_wind_estimator_recovers_steady_wind_from_force_residual():
    p = URBAN_COURIER
    mass = 16.0
    wind = np.array([6.0, -3.0, 0.0])
    est = WindEstimator(p.drag_coefficient(), p.rotor_drag_per_kg)
    rng = np.random.default_rng(0)
    v_ground = np.array([4.0, 2.0, 0.0])
    for _ in range(400):
        v_air = v_ground - wind
        drag = -(p.rotor_drag(mass) + p.drag_coefficient() * np.linalg.norm(v_air)) * v_air
        thrust = -G_VEC - drag / mass  # steady flight: thrust balances gravity + drag
        accel = rng.normal(0, 0.15, 3)
        est.update(v_ground, accel, thrust + rng.normal(0, 0.2, 3), mass, 0.05)
    assert est.converged
    assert np.linalg.norm(est.wind[:2] - wind[:2]) < 0.6
    assert est.gust_std < 0.6
