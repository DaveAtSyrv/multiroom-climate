"""Tests for the Multiroom Climate config flow.

These request ``enable_custom_integrations`` explicitly (rather than an autouse fixture) so the pure
``test_controller.py`` stays Home-Assistant-free.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
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
