"""Tests for the Multiroom Climate entity.

Set the integration up through a real config entry and assert the entity surfaces the weighted
house average, mirrors the wrapped thermostat, reports in the system unit, and exposes a settable
target. (Actuation/no-actuation behaviour is covered in test_coordinator.py.)
"""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

_ENTRY_DATA = {
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room", "sensor.kitchen"],
}


async def _setup(hass: HomeAssistant) -> str:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
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
    # A single settable target alongside the mirrored heat_cool mode — HA renders this without a
    # feature/mode-mismatch warning (the modeling decision, verified in-harness).
    assert state.attributes["supported_features"] == ClimateEntityFeature.TARGET_TEMPERATURE
    assert hass.states.get("climate.daikin").state == "heat_cool"


async def test_exposes_wrapped_band_as_attributes(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    hass.states.async_set("sensor.living_room", "20.0")
    hass.states.async_set("sensor.kitchen", "24.0")
    hass.states.async_set(
        "climate.daikin",
        "heat_cool",
        {"hvac_modes": ["off", "heat_cool"], "target_temp_low": 19.0, "target_temp_high": 23.0},
    )

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.attributes["band_low"] == 19.0
    assert state.attributes["band_high"] == 23.0


async def test_exposes_shadow_decision_attributes(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
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

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    # The controller's decision is surfaced for observability; with the switch off, nothing is written.
    assert state.attributes["shadow_target"] == 70.0
    assert state.attributes["shadow_status"] == "within_deadband"
    assert state.attributes["shadow_sensors_fresh"] == 2
    assert state.attributes["shadow_sensors_total"] == 2
    assert "shadow_learned_offset" in state.attributes
    # Both rooms at 70 → spread 0 → fan would hand back to auto (it's the shadow decision only).
    assert state.attributes["shadow_spread"] == 0.0
    assert state.attributes["shadow_fan_status"] == "spread_low"
    assert hass.states.get("climate.daikin").state == "heat_cool"


async def test_reports_in_system_unit(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # A Fahrenheit household: sensors are already in °F, so the average must pass through as-is —
    # a hardcoded Celsius unit would make HA convert and mislabel it.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.living_room", "68.0")
    hass.states.async_set("sensor.kitchen", "72.0")
    hass.states.async_set("climate.daikin", "heat_cool", {"hvac_modes": ["off", "heat_cool"]})

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.attributes["current_temperature"] == 70.0


async def test_available_with_stale_sensors_when_thermostat_present(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # No fresh sensors but the thermostat is reachable: the entity stays available (so its status is
    # visible) with an unknown current_temperature, rather than vanishing.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.living_room", "unavailable")
    hass.states.async_set("sensor.kitchen", "unknown")
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

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes.get("current_temperature") is None
    # Never regulated yet (no reading), so it's waiting rather than in failsafe.
    assert state.attributes["shadow_status"] == "waiting_for_first_reading"
    assert state.attributes["shadow_sensors_fresh"] == 0


async def test_unavailable_when_thermostat_missing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # The wrapped thermostat isn't in the state machine at all → nothing to observe or control.
    hass.states.async_set("sensor.living_room", "20.0")
    hass.states.async_set("sensor.kitchen", "24.0")

    entity_id = await _setup(hass)
    state = hass.states.get(entity_id)

    assert state is not None
    assert state.state == "unavailable"


async def test_set_temperature_updates_target(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
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
    entity_id = await _setup(hass)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": 72.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["temperature"] == 72.0  # the single settable setpoint


async def test_remove_entry_deletes_stored_state(
    hass: HomeAssistant, hass_storage
) -> None:
    from custom_components.multiroom_climate import async_remove_entry
    from custom_components.multiroom_climate.coordinator import build_store

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
    entry.add_to_hass(hass)
    store = build_store(hass, entry)
    await store.async_save(
        {"learned_offset": -1.0, "target": 70.0, "last_target": 70.0, "last_change_ts": 0.0}
    )
    assert store.key in hass_storage

    await async_remove_entry(hass, entry)

    # The .storage file must not orphan when the entry is deleted.
    assert store.key not in hass_storage
