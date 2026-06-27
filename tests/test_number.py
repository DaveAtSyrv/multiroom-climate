"""Tests for the learned-offset override numbers (the manual escape hatch for the cool/heat offsets)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate import number as number_platform
from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

_ENTRY_DATA = {
    CONF_CLIMATE_ENTITY: "climate.daikin",
    CONF_TARGET_SENSORS: ["sensor.living_room"],
}

# (translation_key / unique-id suffix, coordinator setter, coordinator getter). Parametrizes every
# test over both offsets so the cool and heat numbers are covered identically.
_OFFSETS = [
    ("cool_offset", "async_set_cool_offset", "cool_offset"),
    ("heat_offset", "async_set_heat_offset", "heat_offset"),
]


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
            "hvac_action": "cooling",
        },
    )


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="climate.daikin", data=_ENTRY_DATA, title="Downstairs"
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(("key", "setter", "getter"), _OFFSETS)
async def test_coordinator_set_offset_persists_and_reads_back(
    hass: HomeAssistant, enable_custom_integrations, key: str, setter: str, getter: str
) -> None:
    _seed_states(hass)
    entry = _make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    # An override moves the offset to (approximately) the requested value; the slow EMA then refines
    # it from there (the active-regime offset gets nudged by alpha toward the measured band-vs-house gap).
    await getattr(coordinator, setter)(-3.5)
    await hass.async_block_till_done()
    assert getattr(coordinator, getter) == pytest.approx(-3.5, abs=0.2)


@pytest.mark.parametrize(("key", "setter", "getter"), _OFFSETS)
async def test_offset_number_disabled_by_default(
    hass: HomeAssistant, enable_custom_integrations, key: str, setter: str, getter: str
) -> None:
    # Advanced escape hatches — present in the registry but disabled unless the user enables them.
    _seed_states(hass)
    entry = _make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    assert registry.async_get(entity_id).disabled_by is not None


@pytest.mark.parametrize(("key", "setter", "getter"), _OFFSETS)
async def test_offset_number_overrides_and_resets(
    hass: HomeAssistant, enable_custom_integrations, key: str, setter: str, getter: str
) -> None:
    _seed_states(hass)
    entry = _make_entry(hass)
    # Pre-register the entity enabled so it gets a live state we can drive via the service.
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry, disabled_by=None
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None

    # Drive a corrupted offset in through the entity, confirm both coordinator and displayed state...
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": -9.0}, blocking=True
    )
    assert getattr(coordinator, getter) < -8.0
    assert float(hass.states.get(entity_id).state) < -8.0

    # ...then reset it to ~0 through the same entity (no delete+re-add needed); corruption cleared.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 0.0}, blocking=True
    )
    assert getattr(coordinator, getter) == pytest.approx(0.0, abs=0.3)
    assert float(hass.states.get(entity_id).state) == pytest.approx(0.0, abs=0.3)


def test_parallel_updates_zero_for_coordinator_platform() -> None:
    assert number_platform.PARALLEL_UPDATES == 0


def test_icons_json_keyed_by_number_translation_keys() -> None:
    icons_path = Path(number_platform.__file__).parent / "icons.json"
    icons = json.loads(icons_path.read_text())
    number_icons = icons["entity"]["number"]
    # Both offset numbers have an icon, keyed by their translation_key (hassfest enforces the match).
    assert "cool_offset" in number_icons
    assert "heat_offset" in number_icons
