"""End-to-end closed-loop missions.

Fast scenarios always run; the full matrix is behind ``--runslow`` (each
mission simulates several minutes of flight at 20 Hz).
"""

import pytest

from aidrone.scenarios import SCENARIOS, evaluate, run

FAST = ["clear_day", "spoofed_marker", "hurricane_no_go", "freezing_rain_no_go"]


@pytest.mark.parametrize("name", FAST)
def test_fast_scenarios(name):
    res = run(name)
    ok, why = evaluate(res)
    assert ok, f"{name}: {why}\n{res.summary()}"


@pytest.mark.slow
@pytest.mark.parametrize("name", [n for n in SCENARIOS if n not in FAST])
def test_full_scenario_matrix(name):
    res = run(name)
    ok, why = evaluate(res)
    assert ok, f"{name}: {why}\n{res.summary()}"


def test_clear_day_precision_and_trace():
    res = run("clear_day")
    assert res.delivered and res.delivery_error_m < 0.2
    phases = [e.message for e in res.events if e.category == "phase"]
    assert any("complete" in m for m in phases)
    assert any(e.category == "security" and "verified" in e.message for e in res.events)


def test_spoofed_marker_never_lands():
    res = run("spoofed_marker")
    assert not res.delivered
    assert res.security_rejections and "signature" in res.security_rejections[0]
    assert res.final_phase == "complete"  # flew home with the parcel


def test_no_go_weather_never_launches():
    res = run("hurricane_no_go")
    assert res.final_phase == "rejected" and res.flight_time_s == 0
