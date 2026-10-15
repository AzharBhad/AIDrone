import numpy as np

from aidrone.localization import NavFilter
from aidrone.measurements import BaroMeasurement, GnssFix, TagDetection, UwbRanges, VioMeasurement


def _fix(p, kind="rtk_fixed", sigma=0.03):
    return GnssFix(np.asarray(p, float), sigma, sigma * 2, kind, 30, 0.7)


def test_rtk_tracks_and_integrity_ok():
    nav = NavFilter()
    nav.initialize(np.zeros(3))
    rng = np.random.default_rng(1)
    v = np.array([5.0, 0, 0])
    for k in range(1, 200):
        nav.predict(np.zeros(3), 0.05)
        if k % 4 == 0:
            nav.update_gnss(_fix(v * k * 0.05 + rng.normal(0, 0.02, 3)))
        nav.update_vio(VioMeasurement(v + rng.normal(0, 0.05, 3), 0.06, 1.0))
    sol = nav.solution()
    assert np.linalg.norm(sol.position - v * 199 * 0.05) < 0.2
    assert sol.integrity_ok


def test_nlos_gnss_jump_is_rejected():
    nav = NavFilter()
    nav.initialize(np.zeros(3))
    for _ in range(20):
        nav.predict(np.zeros(3), 0.05)
        nav.update_vio(VioMeasurement(np.zeros(3), 0.05, 1.0))
        nav.update_gnss(_fix([0, 0, 0]))
    accepted = nav.update_gnss(_fix([25.0, 0, 0], "rtk_float", 0.4))  # 25 m multipath jump
    assert not accepted
    assert np.linalg.norm(nav.position) < 0.2


def test_gnss_denied_vio_baro_keeps_bounded_error():
    nav = NavFilter()
    nav.initialize(np.zeros(3), baro_alt=0.0)
    rng = np.random.default_rng(2)
    truth = np.zeros(3)
    v = np.array([3.0, 1.0, 0.0])
    for _ in range(600):  # 30 s without GNSS
        truth = truth + v * 0.05
        nav.predict(rng.normal(0, 0.1, 3), 0.05)
        nav.update_vio(VioMeasurement(v + rng.normal(0, 0.06, 3), 0.08, 0.8))
        nav.update_baro(BaroMeasurement(truth[2] + rng.normal(0, 0.25), 0.4))
    assert np.linalg.norm(nav.position - truth) < 3.0
    assert nav.mode() == "VIO (GNSS-denied)"


def test_uwb_and_tag_give_terminal_precision():
    pad = np.array([10.0, 10.0, 0.0])
    anchors = np.array([pad + d for d in ([2.5, 2.5, 0.3], [-2.5, 2.5, 1.8], [-2.5, -2.5, 0.3], [2.5, -2.5, 1.2])])
    truth = pad + np.array([0.3, -0.2, 6.0])
    nav = NavFilter()
    nav.initialize(truth + np.array([1.5, 1.0, 0.5]))
    nav.P[:3, :3] = np.eye(3) * 4.0
    rng = np.random.default_rng(3)
    for _ in range(100):
        nav.predict(np.zeros(3), 0.05)
        nav.update_vio(VioMeasurement(np.zeros(3), 0.05, 1.0))
        r = np.linalg.norm(anchors - truth, axis=1) + rng.normal(0, 0.08, 4)
        nav.update_uwb(UwbRanges(anchors, r, 0.1))
        nav.update_tag(TagDetection(pad - truth + rng.normal(0, 0.03, 3), 0.04, b""), pad)
    nav.phase = "landing"
    sol = nav.solution()
    assert np.linalg.norm(sol.position - truth) < 0.1
    assert sol.integrity_ok


def test_filter_recovers_from_divergence():
    nav = NavFilter()
    nav.initialize(np.zeros(3))
    nav.P[:3, :3] = np.eye(3) * 1e-4  # over-confident and wrong
    nav.x[:3] = [40.0, 0.0, 0.0]
    for _ in range(200):
        nav.predict(np.zeros(3), 0.05)
        nav.update_gnss(_fix([0, 0, 0]))
    assert nav.resets >= 1
    assert np.linalg.norm(nav.position) < 0.5
