"""Tests for Multiroom Climate diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)
from custom_components.multiroom_climate.diagnostics import (
    async_get_config_entry_diagnostics,
)

_ENTRY_DATA = {
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room", "sensor.kitchen"],
}


async def test_diagnostics_dump(hass: HomeAssistant, enable_custom_integrations) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.living_room", "70.0")
    hass.states.async_set("sensor.kitchen", "70.0")
    hass.states.async_set(
        "climate.daikin",
        "heat_cool",
        {
            "hvac_modes": ["off", "heat_cool"],
            "target_temp_low": 67.0,
            "target_temp_high": 69.0,
            "min_temp": 45.0,
            "max_temp": 95.0,
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"][CONF_CLIMATE_ENTITY] == "climate.daikin"
    assert "learned_offset" in result["control_state"]
    # The last computed tick: both rooms at 70 vs band center 68 → settled within deadband.
    assert result["last_tick"]["status"] == "within_deadband"
    assert result["last_tick"]["thermostat_present"] is True
    # The payload is downloaded as JSON, so it must serialize cleanly.
    json.dumps(result)


async def test_diagnostics_before_first_tick_is_safe(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # If data isn't available yet, last_tick is None rather than raising.
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator.data = None  # simulate "no computed tick yet"

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["last_tick"] is None
    json.dumps(result)
