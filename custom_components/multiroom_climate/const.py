"""Constants for the Multiroom Climate integration."""

DOMAIN = "multiroom_climate"

# Config-entry keys (the minimal set needed to install; tunables live in options).
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_TARGET_SENSORS = "target_sensors"
CONF_HUMIDITY_SENSOR = "humidity_sensor"  # optional; enables cooling-season overcool when set

# Options keys — day/night schedule (the first options-flow surface; see config_flow's OptionsFlow).
# Day/night temps are stored in the *system unit* (the flow collects and labels them that way, so no
# runtime conversion); start times are "HH:MM:SS" wall-clock strings, lead is whole minutes.
CONF_SCHEDULE_ENABLED = "schedule_enabled"
CONF_DAY_TEMP = "day_temp"
CONF_NIGHT_TEMP = "night_temp"
CONF_DAY_START = "day_start"
CONF_NIGHT_START = "night_start"
CONF_OPTIMAL_START_LEAD = "optimal_start_lead"
