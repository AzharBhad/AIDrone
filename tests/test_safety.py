import math

import numpy as np
import pytest

from aidrone.citymap import CityMap
from aidrone.config import URBAN_COURIER
from aidrone.geometry import vec
from aidrone.localization import NavSolution
from aidrone.safety import Battery, EnergyModel, SafetyAction, SafetySupervisor, VehicleHealth
from aidrone.weather import WeatherState, assess_weather


@pytest.fixture(scope="module")
def city():
    return CityMap.generate(seed=7)


def _nav(pos, ok=True):
    return NavSolution(pos, np.zeros(3), np.eye(7) * 0.01, 0.5 if ok else 30.0, 0.5, ["gnss"], ok, 10.0)


def _health(**kw):
    d = dict(motors_ok=6, motor_count=6, thrust_to_weight=2.0, link_age_s=0.0, descent_rate=0.0, commanded_descent=0.0)
    d.update(kw)
    return VehicleHealth(**d)


def _eval(city, pos=None, weather=None, battery_wh=1500.0, need=200.0, **hkw):
    sup = SafetySupervisor(city, URBAN_COURIER)
    pos = pos if pos is not None else vec(300, 300, 100)
    w = assess_weather(weather or WeatherState(), URBAN_COURIER, airborne=True)
    return sup.evaluate(0.0, pos, np.zeros(3), _nav(pos), w, Battery(1600, battery_wh), 1600.0, need,
                        _health(**hkw), 16.5, np.zeros(3))


def test_nominal_continue(city):
    d = _eval(city)
    assert d.action == SafetyAction.CONTINUE


def test_headwind_costs_more_than_tailwind():
    em = EnergyModel(URBAN_COURIER)
    a, b = vec(0, 0, 100), vec(1000, 0, 100)
    e_head, _ = em.leg(a, b, 16.5, vec(-10, 0, 0), 15)
    e_tail, _ = em.leg(a, b, 16.5, vec(10, 0, 0), 15)
    assert e_head > 1.3 * e_tail
    e_imp, _ = em.leg(a, b, 16.5, vec(-30, 0, 0), 15)
    assert math.isinf(e_imp)


def test_cold_battery_derating():
    em = EnergyModel(URBAN_COURIER)
    assert em.usable_wh(-15) < em.usable_wh(20) == URBAN_COURIER.battery_wh


def test_loss_of_lift_deploys_parachute(city):
    d = _eval(city, thrust_to_weight=0.8, motors_ok=4)
    assert d.action == SafetyAction.PARACHUTE


def test_single_motor_failure_diverts_to_safe_site(city):
    d = _eval(city, motors_ok=5, thrust_to_weight=1.5)
    assert d.action == SafetyAction.DIVERT_LAND
    assert d.landing_site is not None
    assert city.population_at(*d.landing_site[:2]) < 0.6


def test_energy_margin_triggers_return(city):
    d = _eval(city, battery_wh=520.0, need=300.0)
    assert d.action in (SafetyAction.RETURN, SafetyAction.DIVERT_LAND)


def test_lightning_forces_landing(city):
    d = _eval(city, weather=WeatherState(lightning_km=3.0))
    assert d.action == SafetyAction.DIVERT_LAND


def test_link_loss_is_handled_autonomously(city):
    assert _eval(city, link_age_s=30).action == SafetyAction.CONTINUE
    assert _eval(city, link_age_s=200).action == SafetyAction.RETURN


def test_geofence_hold(city):
    hx, hy = city.no_fly[0].center
    d = _eval(city, pos=vec(hx, hy, 80))
    assert d.action == SafetyAction.HOLD
