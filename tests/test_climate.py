"""Tests for the read-only Multiroom Climate entity.

Set the integration up through a real config entry and assert the entity surfaces the weighted
house average and mirrors the wrapped thermostat — and writes nothing back to it.
"""

from __future__ import annotations

from homeassistant.components.climate import HVACMode
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

_ENTRY_DATA = {
    CONF_NAME: "Downstairs",
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room", "sensor.kitchen"],
}


async def _setup(hass: HomeAssistant) -> str:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return "climate.downstairs"


async def test_reports_house_average_and_mirrors_mode(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.states.async_set("sensor.living_room", "20.0")
    hass.states.async_set("sensor.kitchen", "24.0")
    hass.states.async_set(
        "climate.daikin", "heat_cool", {"hvac_modes": ["off", "heat_cool", "heat", "cool"]}
    )

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes["current_temperature"] == 22.0
    # Read-only: it advertises no setpoint features and never wrote to the wrapped thermostat.
    assert state.attributes["supported_features"] == 0
    assert hass.states.get("climate.daikin").state == "heat_cool"


async def test_unavailable_when_no_sensor_readings(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.states.async_set("sensor.living_room", "unavailable")
    hass.states.async_set("sensor.kitchen", "unknown")
    hass.states.async_set("climate.daikin", "off", {"hvac_modes": ["off", "heat_cool"]})

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.state == "unavailable"
