import numpy as np

from aidrone.citymap import CityMap
from aidrone.measurements import CameraDetection, ImuSample, RadarTrack, RangefinderMeasurement, SensorFrame
from aidrone.perception import PerceptionFusion, PerceivedWorld, landing_zone_clear, touchdown_votes


def _empty_city():
    return CityMap(np.zeros((100, 100)), 2.0, np.ones((100, 100)))


def _frame(t, lidar=None, radar=(), camera=()):
    return SensorFrame(t=t, imu=ImuSample(np.zeros(3), np.zeros(3)), lidar_points=lidar, radar=list(radar),
                       camera=list(camera))


W = {"lidar": 1.0, "camera": 1.0, "radar": 1.0}


def test_rain_clutter_rejected_but_real_obstacle_confirmed():
    pf = PerceptionFusion(_empty_city())
    drone = np.array([100.0, 100.0, 50.0])
    rng = np.random.default_rng(0)
    obstacle = np.array([[12.0, 0.0, 0.0], [12.0, 0.5, 0.0], [12.0, 1.0, 0.5]])  # relative to drone
    world = None
    for k in range(4):
        rain = rng.normal(size=(40, 3))
        rain = rain / np.linalg.norm(rain, axis=1, keepdims=True) * rng.uniform(1.5, 8, (40, 1))
        world = pf.update(_frame(k * 0.1, np.vstack([obstacle, rain])), drone, W)
    confirmed = world.static_points
    assert len(confirmed) >= 1
    d = np.linalg.norm(confirmed - (drone + obstacle[0]), axis=1)
    assert d.min() < 2.5
    # clutter does not persist: everything confirmed is near the real obstacle
    assert np.all(np.linalg.norm(confirmed - (drone + obstacle[1]), axis=1) < 4.0)
    assert world.clutter_rejected >= 35  # per scan: (almost) every raindrop rejected


def test_ground_returns_explained_by_map():
    pf = PerceptionFusion(_empty_city())
    drone = np.array([50.0, 50.0, 20.0])
    ground = np.array([[x, y, -20.0] for x in range(-5, 6) for y in range(-5, 6)], dtype=float)
    for k in range(3):
        world = pf.update(_frame(k * 0.1, ground), drone, W)
    assert len(world.static_points) == 0


def test_any_modality_blocks_landing_zone():
    pad = np.array([10.0, 10.0, 0.0])
    w = PerceivedWorld(np.zeros((0, 3)), people=[CameraDetection(pad + [1, 0, 0], "person", 0.9)])
    clear, reasons = landing_zone_clear(w, pad, 4.0, 0.0)
    assert not clear and "person" in reasons[0]
    pf = PerceptionFusion(_empty_city())
    drone = pad + np.array([0, 0, 15.0])
    w2 = pf.update(_frame(0, radar=[RadarTrack(np.array([0.5, 0.5, -14.5]), np.zeros(3), 0.5)]), drone, W)
    assert not landing_zone_clear(w2, pad, 4.0, 0.0)[0]
    assert landing_zone_clear(PerceivedWorld(np.zeros((0, 3))), pad, 4.0, 0.0)[0]


def test_touchdown_needs_consensus_not_one_sensor():
    # Rain droplet makes the rangefinder read 0.2 m while the aircraft is 6 m up.
    rain = touchdown_votes(RangefinderMeasurement(0.2), height_above_pad=6.0, vertical_speed=-0.7, thrust_ratio=1.0)
    assert rain.count == 1 and not rain.consensus
    landed = touchdown_votes(RangefinderMeasurement(0.05), height_above_pad=0.1, vertical_speed=0.0, thrust_ratio=0.4)
    assert landed.consensus
    # Even with a failed rangefinder the other three cues suffice.
    no_rf = touchdown_votes(None, height_above_pad=0.1, vertical_speed=0.0, thrust_ratio=0.4)
    assert no_rf.consensus
