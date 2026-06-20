"""Tests for the master enable switch (the kill switch)."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.multiroom_climate import switch as switch_platform
from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

_SWITCH_ID = "switch.downstairs_control"
_ENTRY_DATA = {
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room"],
}


def _seed_states(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.living_room", "70.0")
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


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_switch_defaults_off_and_toggles_coordinator(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    _seed_states(hass)
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    # A fresh install is inert: switch off, coordinator not actuating.
    assert hass.states.get(_SWITCH_ID).state == STATE_OFF
    assert coordinator.enabled is False

    await hass.services.async_call(
        Platform.SWITCH, "turn_on", {"entity_id": _SWITCH_ID}, blocking=True
    )
    assert coordinator.enabled is True
    assert hass.states.get(_SWITCH_ID).state == STATE_ON

    await hass.services.async_call(
        Platform.SWITCH, "turn_off", {"entity_id": _SWITCH_ID}, blocking=True
    )
    assert coordinator.enabled is False
    assert hass.states.get(_SWITCH_ID).state == STATE_OFF


async def test_switch_restores_enabled_state(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # Simulate the switch having been left on before a restart.
    mock_restore_cache(hass, [State(_SWITCH_ID, STATE_ON)])
    _seed_states(hass)
    entry = await _setup(hass)

    assert hass.states.get(_SWITCH_ID).state == STATE_ON
    assert entry.runtime_data.enabled is True


def test_parallel_updates_zero_for_coordinator_platform() -> None:
    # Toggling only flips an in-memory flag; the coordinator owns all I/O. Pin the quality-bar value.
    assert switch_platform.PARALLEL_UPDATES == 0
