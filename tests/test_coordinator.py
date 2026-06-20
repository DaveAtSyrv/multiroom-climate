"""Tests for the Multiroom Climate coordinator.

The pure ``house_average`` helper is tested directly; the sensor-filtering path is exercised
through a live coordinator reading ``hass.states`` so the "ignore unavailable / non-numeric"
behaviour is covered end to end.
"""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

from custom_components.multiroom_climate.const import (
    CONF_CLIMATE_ENTITY,
    CONF_TARGET_SENSORS,
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
    entry = type(
        "Entry",
        (),
        {
            "title": "Test",
            "data": {
                CONF_NAME: "Test",
                CONF_CLIMATE_ENTITY: "climate.daikin",
                CONF_TARGET_SENSORS: sensors,
            },
        },
    )()
    return MultiroomClimateCoordinator(hass, entry)


async def test_update_averages_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "24.0")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data == {"house_average": 22.0, "available": True}


async def test_update_skips_unavailable_and_non_numeric(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "unavailable")
    hass.states.async_set("sensor.c", "comfy")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b", "sensor.c", "sensor.missing"])

    data = await coordinator._async_update_data()

    assert data == {"house_average": 20.0, "available": True}


async def test_update_unavailable_when_no_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "unavailable")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data == {"house_average": None, "available": False}
