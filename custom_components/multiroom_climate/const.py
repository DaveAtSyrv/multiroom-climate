"""Constants for the Multiroom Climate integration."""

DOMAIN = "multiroom_climate"

# Config-entry keys (the minimal set needed to install; tunables live in options later).
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_TARGET_SENSORS = "target_sensors"
CONF_HUMIDITY_SENSOR = "humidity_sensor"  # optional; enables cooling-season overcool when set
