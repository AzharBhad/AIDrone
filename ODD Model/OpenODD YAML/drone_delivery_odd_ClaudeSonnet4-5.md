******
  # =====================================
  # TAXONOMY DEFINITION
  # =====================================
## 1. Geographic and Spatial Attributes
description: "Spatial boundaries and geographic constraints"

1.1 operational_area:
          description: "Geographic zone classification"

1.2 altitude_range:
          description: "Operating altitude above ground level (AGL)"

1.3 geofence_boundary:
          description: "Geographic boundary polygon definition"

1.4 terrain_type:
          description: "Ground terrain characteristics"

1.5 landing_zone:
          description: "Delivery destination characteristics"

## 2. Environmental Conditions
description: "Atmospheric and environmental operating conditions"

2.1         weather_conditions:
          description: "Meteorological parameters"

    Visibility
    precipitation
    wind_conditions
    temperature
    cloud_conditions

2.2 lighting_conditions:
          description: "Natural and artificial illumination"

2.3 air_quality

## 3. Dynamic Environment
dynamic_elements:
      description: "Moving objects and traffic in operational environment"

3.1 aerial_traffic
description: "Other aircraft and aerial vehicles"

    cooperative_aircraft
    non_cooperative_aircraft
    traffic_density
    minimum_separation

3.2 ground_obstacles:
          description: "Stationary and moving ground objects"

    static_obstacles
    dynamic_obstacles
    electromagnetic_interference

## 4. Operational Parameters
operational_domain:
      description: "System operational characteristics and constraints"

4.1 mission_profile

    mission_type
    flight_distance
    flight_duration
    payload_weight
    package_dimensions
    

4.2 vehicle_capabilities:

    propulsion_type
    redundancy_level
    positioning_systems
    detect_and_avoid
    communication_systems

4.3 regulatory_compliance:
          description: "Legal and regulatory requirements"

    airspace_authorization
    remote_identification
    beyond_visual_line_of_sight
    population_density
    privacy_compliance

4.4 time_of_day

    operational_hours
    local_time_restrictions

## 5. Safety and Emergency
safety_domain:
      description: "Safety-critical operational constraints"

5.1 emergency_procedures

    lost_link_behavior
    low_battery_behavior
    geofence_breach
    collision_avoidance
        avoidance_strategy
        minimum_detection_range

5.2 safe_landing_zones

    pre_designated_zones
    dynamic_zone_selection
    minimum_zone_size

5.3 failure_modes
    
    single_motor_failure
    gps_loss
    communication_loss
    sensor_degradation

  # =====================================
  # ODD MODULES - BOUNDARY CONDITIONS
  # =====================================
******