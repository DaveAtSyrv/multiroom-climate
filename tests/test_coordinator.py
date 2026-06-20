"""Tests for the Multiroom Climate coordinator.

The pure ``house_average`` helper is tested directly; the sensor + wrapped-thermostat read paths
are exercised through a live coordinator reading ``hass.states`` so the "ignore unavailable /
non-numeric / unknown HVAC mode" behaviour is covered end to end.
"""

from __future__ import annotations

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
    DOMAIN,
)
from custom_components.multiroom_climate.coordinator import (
    MultiroomClimateCoordinator,
    house_average,
)


def test_house_average_equal_weight() -> None:
    assert house_average([20.0, 22.0]) == 21.0


def test_house_average_single_sensor() -> None:
    assert house_average([19.5]) == 19.5


def test_house_average_empty_is_none() -> None:
    assert house_average([]) is None


def _make_coordinator(hass: HomeAssistant, sensors: list[str]) -> MultiroomClimateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: sensors},
    )
    return MultiroomClimateCoordinator(hass, entry)


async def test_update_averages_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "24.0")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data.house_average == 22.0
    assert data.available is True


async def test_update_skips_unavailable_and_non_numeric(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "unavailable")
    hass.states.async_set("sensor.c", "comfy")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b", "sensor.c", "sensor.missing"])

    data = await coordinator._async_update_data()

    assert data.house_average == 20.0
    assert data.available is True


async def test_update_unavailable_when_no_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "unavailable")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.house_average is None
    assert data.available is False


async def test_mirrors_wrapped_mode_and_drops_unknown_modes(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set(
        "climate.daikin", "heat_cool", {"hvac_modes": ["off", "heat_cool", "bogus"]}
    )
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.hvac_mode is HVACMode.HEAT_COOL
    # "bogus" isn't a real HVACMode, so it's filtered out of the mirrored list.
    assert data.hvac_modes == (HVACMode.OFF, HVACMode.HEAT_COOL)


async def test_wrapped_missing_yields_no_mode(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    coordinator = _make_coordinator(hass, ["sensor.a"])  # no climate.daikin in the state machine

    data = await coordinator._async_update_data()

    assert data.hvac_mode is None
    assert data.hvac_modes == ()
