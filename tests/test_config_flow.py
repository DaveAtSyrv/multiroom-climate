"""Tests for the Multiroom Climate config flow.

These request ``enable_custom_integrations`` explicitly (rather than an autouse fixture) so the pure
``test_controller.py`` stays Home-Assistant-free.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_DAY_START,
    CONF_DAY_TEMP,
    CONF_HUMIDITY_SENSOR,
    CONF_NIGHT_START,
    CONF_NIGHT_TEMP,
    CONF_OPTIMAL_START_LEAD,
    CONF_SCHEDULE_ENABLED,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

_USER_INPUT = {
    CONF_NAME: "Downstairs",
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room_temperature", "sensor.kitchen_temperature"],
}


async def test_user_flow_creates_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Downstairs"
    assert result["data"][CONF_CLIMATE_ENTITY] == "climate.daikin"
    assert result["data"][CONF_TARGET_SENSORS] == _USER_INPUT[CONF_TARGET_SENSORS]
    # The dedup key must be derived from the wrapped thermostat.
    assert result["result"].unique_id == "climate.daikin"
    # The humidity sensor is optional — omitting it leaves the key absent (so .get() returns None).
    assert CONF_HUMIDITY_SENSOR not in result["data"]


async def test_user_flow_persists_optional_humidity_sensor(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_USER_INPUT, CONF_HUMIDITY_SENSOR: "sensor.hallway_humidity"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HUMIDITY_SENSOR] == "sensor.hallway_humidity"


async def test_empty_sensors_shows_error(hass: HomeAssistant, enable_custom_integrations) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_USER_INPUT, CONF_TARGET_SENSORS: []}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TARGET_SENSORS: "no_sensors"}


async def test_duplicate_thermostat_is_aborted(hass: HomeAssistant, enable_custom_integrations) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_USER_INPUT
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


_SCHEDULE_INPUT = {
    CONF_SCHEDULE_ENABLED: True,
    CONF_DAY_TEMP: 70.0,
    CONF_NIGHT_TEMP: 64.0,
    CONF_DAY_START: "06:00:00",
    CONF_NIGHT_START: "22:00:00",
    CONF_OPTIMAL_START_LEAD: 45,
}


async def test_options_flow_stores_schedule(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # _SCHEDULE_INPUT uses Fahrenheit setpoints.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="climate.daikin", data=_USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(result["flow_id"], _SCHEDULE_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == _SCHEDULE_INPUT


async def test_options_flow_temp_defaults_follow_fahrenheit(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(domain=DOMAIN, unique_id="climate.daikin", data=_USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Applying the schema to an empty dict fills in every Required default.
    defaults = result["data_schema"]({})
    assert defaults[CONF_DAY_TEMP] == 70.0
    assert defaults[CONF_NIGHT_TEMP] == 64.0


async def test_options_flow_temp_defaults_follow_celsius(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.config.units = METRIC_SYSTEM
    entry = MockConfigEntry(domain=DOMAIN, unique_id="climate.daikin", data=_USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults[CONF_DAY_TEMP] == 21.0
    assert defaults[CONF_NIGHT_TEMP] == 18.0


async def test_options_flow_prefills_saved_values(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # saved _SCHEDULE_INPUT setpoints are Fahrenheit.
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_USER_INPUT, options=_SCHEDULE_INPUT
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = result["data_schema"]({})
    assert defaults[CONF_DAY_TEMP] == 70.0
    assert defaults[CONF_DAY_START] == "06:00:00"
    assert defaults[CONF_OPTIMAL_START_LEAD] == 45


async def test_options_change_reloads_coordinator(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Saving options reloads the entry so the coordinator rebuilds with the new schedule."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.living_room", "70.0")
    hass.states.async_set("sensor.kitchen", "70.0")
    hass.states.async_set("climate.daikin", "heat_cool", {"hvac_modes": ["off", "heat_cool"]})

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="climate.daikin",
        title="Downstairs",
        data={CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: ["sensor.living_room", "sensor.kitchen"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data._config.day_temp == 21.0  # base default before any schedule

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], _SCHEDULE_INPUT)
    await hass.async_block_till_done()

    # The update listener reloaded the entry; the rebuilt coordinator carries the saved schedule.
    assert entry.runtime_data._config.day_temp == 70.0
    assert entry.runtime_data._config.day_start_min == 360.0
