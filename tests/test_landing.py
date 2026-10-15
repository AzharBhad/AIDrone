import numpy as np

from aidrone.config import URBAN_COURIER
from aidrone.geometry import vec
from aidrone.landing import LandingStage, PrecisionLander, WinchController, WinchStage

PAD = vec(10, 10, 0)


def _run(lander, pos, steps, tag=True, gust=0.0, t0=0.0, dt=0.05, **kw):
    cmd = None
    for k in range(steps):
        cmd = lander.update(t0 + k * dt, pos, tag, gust, dt, **kw)
        pos = np.array([pos[0], pos[1], cmd.position[2]])
    return cmd, pos


def test_descends_when_centred_and_reaches_flare():
    l = PrecisionLander(URBAN_COURIER, PAD)
    cmd, pos = _run(l, PAD + vec(0.05, 0, 20), 1200)
    assert pos[2] < 0.5
    assert l.stage in (LandingStage.FLARE, LandingStage.TERMINAL)


def test_gusts_slow_the_descent():
    calm = PrecisionLander(URBAN_COURIER, PAD)
    gusty = PrecisionLander(URBAN_COURIER, PAD)
    _, p_calm = _run(calm, PAD + vec(0, 0, 20), 100)
    _, p_gust = _run(gusty, PAD + vec(0, 0, 20), 100, gust=3.0)
    assert p_gust[2] > p_calm[2]


def test_holds_off_centre_and_goes_around_then_aborts():
    l = PrecisionLander(URBAN_COURIER, PAD, max_go_arounds=2)
    pos = PAD + vec(1.5, 0, 6)  # stuck off-centre at low height
    t = 0.0
    stages = set()
    for _ in range(4000):
        cmd = l.update(t, pos, True, 0.0, 0.05)
        stages.add(cmd.stage)
        pos = vec(pos[0], pos[1], cmd.position[2])  # vehicle follows altitude, wind keeps it off-centre
        t += 0.05
        if cmd.stage == LandingStage.ABORT:
            break
    assert LandingStage.GO_AROUND in stages
    assert l.stage == LandingStage.ABORT and l.go_arounds == 3


def test_needs_visual_fix_in_terminal_stage_unless_waived():
    l = PrecisionLander(URBAN_COURIER, PAD)
    cmd = l.update(0.0, PAD + vec(0, 0, 5), False, 0.0, 0.05)
    assert "visual" in cmd.note and cmd.velocity[2] == 0
    l2 = PrecisionLander(URBAN_COURIER, PAD)
    cmd2 = l2.update(0.0, PAD + vec(0, 0, 5), False, 0.0, 0.05, require_tag=False)
    assert cmd2.velocity[2] < 0


def test_flare_is_committed():
    l = PrecisionLander(URBAN_COURIER, PAD)
    cmd = l.update(0.0, PAD + vec(0.6, 0, 0.5), False, 0.0, 0.05)
    assert cmd.stage == LandingStage.FLARE and cmd.velocity[2] < 0


def test_winch_lowers_detects_ground_releases_and_retracts():
    w = WinchController(drop_height=18.0, payload_mass=2.5, tether_length=25.0)
    weight = 2.5 * 9.81
    payout, t = 0.0, 0.0
    released = False
    for _ in range(4000):
        on_ground = payout >= 18.0
        cmd = w.update(t, payout, 0.0 if on_ground else weight, np.zeros(3), np.zeros(3), 0.05, authorised=True)
        released |= cmd.release
        payout = max(0.0, payout + cmd.payout_rate * 0.05)
        t += 0.05
        if w.stage == WinchStage.DONE:
            break
    assert released and w.stage == WinchStage.DONE


def test_winch_waits_for_authorisation_and_can_abort():
    w = WinchController(18.0, 2.5, 25.0)
    w.stage = WinchStage.ON_GROUND
    cmd = w.update(0, 18.0, 0.0, np.zeros(3), np.zeros(3), 0.05, authorised=False)
    assert not cmd.release and "authorisation" in cmd.note
    w.abort()
    cmd = w.update(0.1, 18.0, 0.0, np.zeros(3), np.zeros(3), 0.05, authorised=True)
    assert not cmd.release and cmd.payout_rate < 0


def test_winch_snag_cuts_line():
    w = WinchController(18.0, 2.5, 25.0)
    cmd = w.update(0, 5.0, 2.5 * 9.81 * 3, np.zeros(3), np.zeros(3), 0.05)
    assert cmd.cut and w.stage == WinchStage.SNAG_CUT


def test_winch_sway_damping_moves_over_parcel():
    w = WinchController(18.0, 2.5, 25.0)
    cmd = w.update(0, 5.0, 2.5 * 9.81, vec(1.0, 0, 0), vec(0.5, 0, 0), 0.05)
    assert cmd.drone_offset[0] > 0.5
