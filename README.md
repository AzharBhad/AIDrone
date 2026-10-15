# AIDrone: all-weather urban precision-delivery autonomy

AIDrone is the companion-computer "brain" for an autonomous delivery drone. It is
built to deliver parcels **precisely** (to centimetres) in **dense cities** and in
**bad weather**, and to know when it should not fly at all. It implements the
architecture designed in [`Documentation/Process`](Documentation/Process) and the
ODD in [`AIDroneOperations`](AIDroneOperations). It is a working Python stack with
a closed-loop physics simulator and tests.

```
python -m aidrone list                # the scenario library
python -m aidrone run urban_storm     # one mission with its full decision log
python -m aidrone all                 # scorecard across weather, faults and attacks
pytest -q                             # unit tests + fast end-to-end missions
pytest -q --runslow                   # + full scenario matrix
```

Requires Python ≥ 3.10, `numpy`, `pyyaml`, `cryptography` (`pip install -e ".[dev]"`).

## What "any weather" means here

No aircraft can safely fly in *literally* any weather. A drone that claims it can
is the one that crashes. This stack takes a different approach:

1. **A very wide envelope.** IP67 airframe, heated battery/props/sensors, radar for
   fog, thermal + NIR for night, a hexacopter that survives a motor failure, and a
   parachute. Flight limits: 15 m/s sustained wind with 21 m/s gusts, 50 mm/h rain,
   10 mm/h snow, −20…50 °C, 100 m visibility (`config/urban_all_weather.yaml`).
2. **Graceful degradation inside it.** `NOMINAL → CAUTION → DEGRADED` means flying
   slower and lower, re-weighting sensors, slowing descents in gusts, adding
   obstacle clearance, and switching on heaters.
3. **Hard, explainable limits at the edge.** Lightning nearby, freezing rain, or wind
   it cannot make headway against → `HOLD` before launch, `RETURN` or `LAND_NOW`
   (at the safest reachable site) in flight. The whole mission must fit inside the
   forecast window, not just the take-off.

## Architecture (doc 1.2 "split brain")

```
 Ground / cloud (fleet.py)                Onboard companion computer (autonomy.py, 20 Hz)
 ─────────────────────────                ──────────────────────────────────────────────
 orders, signed delivery tickets  ──────▶  sense ─▶ localise ─▶ wind ─▶ weather ─▶ perceive
 launch-window weather check               ─▶ safety supervisor ─▶ mission FSM ─▶ plan ─▶ setpoint
                                                                                     │
                                            PX4 flight controller (flight_controller.py)
                                            TrajectorySetpoint NED @ ≥2 Hz, offboard-loss failsafe
```

| Module | What it does | Design doc |
|---|---|---|
| `localization.py` | EKF fusing RTK-GNSS / VIO / baro / UWB / AprilTag. χ² gating rejects multipath and NLOS jumps. Per-sensor health, protection levels against per-phase alert limits, divergence recovery. | 1.3 |
| `weather.py` | Urban wind field (power law, street-canyon channelling, turbulence). Wind and gust estimation from force residuals (no pitot). Graded go / slow / return / land assessment and sensor de-rating. | 1.11 |
| `perception.py` | Map background subtraction, rain/snow clutter removal (range-adaptive outlier filter + temporal persistence), LiDAR/radar/camera fusion. **Hazard if *any* sensor sees it; irreversible actions need *consensus*** (Amazon MK30 lesson). | 1.4, 1.14 |
| `planning.py` | 3D A* weighted by **ground risk** (population density), **wind exposure** and altitude preference, with clearance that inflates with gusts. DWA + velocity-obstacle local planner, braking-distance speed cap, replanning around unmapped obstacles. | 1.5, 1.9 |
| `landing.py` | Two-stage precision landing (UWB approach → vision terminal), gust-scaled descent, tightening lateral funnel, go-arounds. **Winch/tether delivery** with sway damping, touchdown and snag detection for courtyards, balconies and crowded gardens. | 1.8 |
| `security.py` | Ed25519-signed landing markers bound to order, site, surveyed position, expiry and nonce. Rejects counterfeit, replayed, expired and **relocated** markers. HOTP one-time code from the recipient before release. | 1.8, 1.10 |
| `safety.py` | Wind-aware energy model and **energy-margin** battery failsafes, geofence, tiered L1/L2/L3 failsafes (`safety_protocols.yaml`), lowest-risk contingency landing site, parachute. | 1.14 |
| `autonomy.py` | Mission state machine and failsafe arbitration, with an explainable decision log for every choice. | 1.2 |
| `flight_controller.py` | PX4 `TrajectorySetpoint` boundary (ENU→NED, NaN = uncontrolled), offboard-heartbeat failsafe, `Px4Ros2Bridge` for real hardware. | 1.2, 1.7 |
| `sim/` | Truth city, multirotor physics (rotor + body drag, thrust/tilt limits, motor loss, parachute), tether pendulum, and sensor models with weather and urban-canyon degradation. | 1.15 |

### Urban-specific intelligence

- **Urban canyons:** GNSS is modelled by ray-casting satellites against buildings. The
  filter detects multipath and NLOS jumps and rejects them, VIO and baro carry the
  solution, and UWB and the marker take over for the last metres.
- **Ground risk:** routes prefer rooftops and parks over busy streets, and keep out of
  hospital/heliport no-fly zones.
- **Street wind:** the wind model channels along streets and becomes rougher in
  building wakes. The vehicle estimates the actual wind and feeds it forward to the
  flight controller.
- **Tight drop points:** if the site is too tight or too crowded to land, the parcel is
  lowered on a tether from a safe hover height that is checked against the 3D map.
- **People:** a person on the pad pauses the descent; a pad blocked through three
  waiting windows aborts the delivery (`4.delivery_zone.mmd`).

## Scenario scorecard

| Scenario | Outcome | Placement error | What it proves |
|---|---|---|---|
| clear_day | delivered (landed) | 4.6 cm | Baseline: RTK→UWB→vision landing, OTP release, return home |
| urban_storm | delivered (landed) | 5.9 cm | 30 mm/h rain plus gusts: cruises lower (winds aloft), rain-clutter filtering, false rangefinder "ground" readings ignored |
| night_fog | delivered (winch) | 39 cm | Night with 180 m visibility: thermal/NIR/radar, tethered drop into a courtyard |
| winter_snow | delivered (landed) | 12.4 cm | −8 °C snow: heaters, cold-battery derating |
| gnss_denied_canyon | delivered (winch) | 123 cm | No RTK, multipath/NLOS in a courtyard: VIO + UWB + vision carry navigation |
| crane_obstacle | delivered (landed) | 2.7 cm | Unmapped tower crane found by perception in flight, replanned around |
| drone_traffic | delivered (landed) | 8.5 cm | Head-on uncooperative drone: velocity-obstacle avoidance (closest approach 10.6 m) |
| person_on_pad | delivered (landed) | 5.6 cm | Descent paused until the pad is clear |
| spoofed_marker | refused, returned with parcel | – | Counterfeit landing marker rejected (signature) |
| relocated_marker | refused, returned with parcel | – | Genuine marker moved by a thief: rejected |
| thunderstorm_inbound | landed at contingency site | – | Pop-up storm the forecast missed: abort, then lightning land-now at the lowest-risk site |
| hurricane_no_go | not launched | – | Wind beyond envelope |
| freezing_rain_no_go | not launched | – | Icing beyond de-icing capacity |
| motor_failure | landed at contingency site | – | Hexacopter loses a motor: diverts to a park, lands within 3 cm |
| link_loss | delivered (landed) | 4.6 cm | 65 s C2 outage: the aircraft completes the mission autonomously |
| low_battery_headwind | not launched | – | Energy margin (wind-aware) refuses the mission |

**16/16 pass, zero collisions.** Pad landings: 3–12 cm. Winch drops are less precise
(0.4–1.2 m) because the parcel swings on the tether in gusty canyon air. Tighter
sway control there is a known improvement area.

Each mission flies the full loop (pre-flight checks, take-off, cruise, approach, zone
check, marker authentication, delivery, return, landing) in a procedurally generated
downtown at 20 Hz autonomy and 80 Hz physics. The pass criteria are
`scenarios.evaluate`: never collide, deliver when expected, refuse when expected,
and land within 50 cm.

## Vehicle profiles

- `urban_courier` (default): 14 kg hexacopter, 5 kg payload, 1.6 kWh battery, winch,
  radar, thermal camera, parachute.
- `heavy_lift_50kg`: octocopter for the 50 kg / 300 m-ceiling requirement in
  `Documentation/Custom recommendation.md`. Above 120 m AGL it needs a regulatory
  waiver (SORA / Part 107 waiver), so the planner still prefers the cruise altitude.

## Status and honest limitations

This is **simulation-validated autonomy software, not a certified aircraft.**

- The simulator is deliberately mid-fidelity: point-mass dynamics with a PX4-like
  cascaded controller, and sensor models designed to reproduce the documented
  *failure modes* (multipath, rain clutter, fog, NLOS UWB, false rangefinder
  returns) rather than a particular sensor's exact datasheet.
- The next step is software-in-the-loop with PX4 SITL + Gazebo through
  `Px4Ros2Bridge`, then hardware-in-the-loop, then flight test. Each step follows
  `Documentation/Process/1.15 Validation & Verification Pipeline.md` and the
  `ED-324` checklist.
- The ML pieces planned in `ai_decision_system.yaml` (learned path planning, risk
  models) belong behind the same interfaces. The current planners and estimators are
  deterministic and explainable on purpose: a certifiable baseline that learned
  components can be measured against.
