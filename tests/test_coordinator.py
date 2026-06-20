"""Tests for the Multiroom Climate coordinator.

The pure ``house_average`` helper is tested directly; the sensor + wrapped-thermostat read paths
are exercised through a live coordinator reading ``hass.states`` so the "ignore unavailable /
non-numeric / unknown HVAC mode" behaviour is covered end to end.
"""

from __future__ import annotations

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
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


def _make_coordinator(
    hass: HomeAssistant, sensors: list[str], entry_id: str = "test_entry"
) -> MultiroomClimateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        entry_id=entry_id,
        data={CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: sensors},
    )
    return MultiroomClimateCoordinator(hass, entry)


async def test_update_averages_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "24.0")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data.house_average == 22.0


async def test_update_skips_unavailable_and_non_numeric(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("sensor.b", "unavailable")
    hass.states.async_set("sensor.c", "comfy")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b", "sensor.c", "sensor.missing"])

    data = await coordinator._async_update_data()

    assert data.house_average == 20.0


async def test_update_unavailable_when_no_valid_sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "unavailable")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.house_average is None


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
    assert data.band_low is None
    assert data.band_high is None


async def test_reads_wrapped_band_setpoints(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set(
        "climate.daikin",
        "heat_cool",
        {"hvac_modes": ["off", "heat_cool"], "target_temp_low": 19.5, "target_temp_high": 23.0},
    )
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.band_low == 19.5
    assert data.band_high == 23.0


async def test_band_none_when_setpoints_absent(hass: HomeAssistant) -> None:
    # A single-setpoint mode (heat/cool) advertises no AUTO band — band stays None, not a crash.
    hass.states.async_set("sensor.a", "20.0")
    hass.states.async_set("climate.daikin", "heat", {"hvac_modes": ["off", "heat"]})
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.band_low is None
    assert data.band_high is None


def _heat_cool(
    low: float, high: float, *, min_temp: float = 45.0, max_temp: float = 95.0
) -> tuple[str, dict]:
    return "heat_cool", {
        "hvac_modes": ["off", "heat_cool"],
        "target_temp_low": low,
        "target_temp_high": high,
        "min_temp": min_temp,
        "max_temp": max_temp,
    }


async def test_shadow_seeds_target_and_learns_offset_when_settled(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    # Target seeds to the current house average; error 0 → within deadband → learn, propose nothing.
    assert data.target == 70.0
    assert data.proposed is not None
    assert data.proposed.reason == "within_deadband"
    assert data.proposed.set_band is False
    # band_center 68 − house 70 = −2; EMA from 0 with alpha 0.05 → −0.1.
    assert data.learned_offset == pytest.approx(-0.1)


async def test_shadow_proposes_trim_when_house_drifts(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # tick 1 seeds target = 70
    hass.states.async_set("sensor.a", "66.0")  # house drops well below target
    data = await coordinator._async_update_data()  # tick 2

    assert data.target == 70.0
    assert data.proposed is not None
    assert data.proposed.reason == "trim"
    assert data.proposed.set_band is True
    # error 70−66 = 4; step clamp(0.3*4, −0.5, 0.5) = 0.5; band shifts +0.5 (in °F bounds).
    assert data.proposed.band_low == pytest.approx(67.5)
    assert data.proposed.band_high == pytest.approx(69.5)


async def test_shadow_skipped_without_band(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", "heat", {"hvac_modes": ["off", "heat"]})
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    # No AUTO band → decide() can't run → no proposal, target/offset untouched.
    assert data.proposed is None
    assert data.target is None
    assert data.learned_offset == 0.0


async def test_shadow_skipped_without_equipment_bounds(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    # Band present but the thermostat advertises no min_temp/max_temp → can't clamp safely → skip.
    hass.states.async_set(
        "climate.daikin",
        "heat_cool",
        {"hvac_modes": ["off", "heat_cool"], "target_temp_low": 67.0, "target_temp_high": 69.0},
    )
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.proposed is None
    # The band is still surfaced for observability even when decide() is skipped.
    assert data.band_low == 67.0
    assert data.band_high == 69.0


async def test_trim_clamped_to_equipment_max_temp(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    # Equipment max is only 0.3 above the cool setpoint, so an upward trim can move at most 0.3 —
    # proving the bound comes from the thermostat's own max_temp, not the °C default (which would
    # allow the full 0.5 step).
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0, max_temp=69.3))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # tick 1 seeds target = 70
    hass.states.async_set("sensor.a", "60.0")  # cold → upward trim demanded
    data = await coordinator._async_update_data()

    assert data.proposed is not None
    assert data.proposed.reason == "trim"
    assert data.proposed.band_high == pytest.approx(69.3)
    assert data.proposed.band_low == pytest.approx(67.3)


async def test_failsafe_after_sensor_loss(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # tick 1: seed target, learn offset
    learned = coordinator._learned_offset
    hass.states.async_set("sensor.a", "unavailable")  # lose the only sensor
    data = await coordinator._async_update_data()  # tick 2

    assert data.house_average is None
    assert data.status == "failsafe"
    assert data.proposed is not None
    assert data.proposed.set_band is False
    assert data.proposed.notify  # would-notify text, surfaced but not delivered
    assert data.target == 70.0  # target retained across the dropout
    assert coordinator._learned_offset == learned  # never learn off a missing reading


async def test_waiting_for_first_reading(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "unavailable")  # no fresh reading yet
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    # Never regulated → this is "waiting", not a failsafe; target stays unseeded.
    assert data.status == "waiting_for_first_reading"
    assert data.proposed is None
    assert data.target is None


async def test_partial_staleness_regulates_off_survivors(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.b", "unavailable")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    # One sensor down doesn't freeze the HVAC — regulate off the survivor, but show the degradation.
    assert data.house_average == 70.0
    assert data.fresh_sensors == 1
    assert data.total_sensors == 2
    assert data.proposed is not None
    assert data.status == "within_deadband"


async def test_restores_state_on_load(hass: HomeAssistant, hass_storage) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = _make_coordinator(hass, ["sensor.a"])
    hass_storage[coordinator._store.key] = {
        "version": 1,
        "data": {
            "learned_offset": -1.5,
            "target": 71.0,
            "last_target": 71.0,
            "last_change_ts": 0.0,
        },
    }

    await coordinator.async_load_state()

    assert coordinator._learned_offset == -1.5
    assert coordinator._target == 71.0

    # The restored target is not re-seeded to the current house average on the next tick.
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    data = await coordinator._async_update_data()
    assert data.target == 71.0


async def test_load_with_no_stored_state_keeps_defaults(
    hass: HomeAssistant, hass_storage
) -> None:
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator.async_load_state()  # nothing stored

    assert coordinator._learned_offset == 0.0
    assert coordinator._target is None


async def test_saves_and_reloads_control_state(hass: HomeAssistant, hass_storage) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # settled tick seeds target + learns a non-zero offset
    learned = coordinator._learned_offset
    assert learned != 0.0
    await coordinator._store.async_save(coordinator._persisted_state())  # flush the debounced write

    # A fresh coordinator for the same entry restores the persisted control state.
    reloaded = _make_coordinator(hass, ["sensor.a"])
    await reloaded.async_load_state()
    assert reloaded._learned_offset == learned
    assert reloaded._target == 70.0
