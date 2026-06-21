"""Tests for the Multiroom Climate coordinator.

The pure ``house_average`` helper is tested directly; the sensor + wrapped-thermostat read paths
are exercised through a live coordinator reading ``hass.states`` so the "ignore unavailable /
non-numeric / unknown HVAC mode" behaviour is covered end to end.
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

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
from custom_components.multiroom_climate.controller import ControllerConfig
from custom_components.multiroom_climate.coordinator import (
    _THERMOSTAT_MISSING_TICKS,
    MultiroomClimateCoordinator,
    _config_from_options,
    _time_to_minutes,
    house_average,
    spread,
    thermostat_missing_issue_id,
)


def test_house_average_equal_weight() -> None:
    assert house_average([20.0, 22.0]) == 21.0


def test_house_average_single_sensor() -> None:
    assert house_average([19.5]) == 19.5


def test_house_average_empty_is_none() -> None:
    assert house_average([]) is None


def test_spread_is_max_minus_min() -> None:
    assert spread([20.0, 24.0, 22.0]) == 4.0


def test_spread_single_sensor_is_none() -> None:
    assert spread([21.0]) is None


def test_spread_empty_is_none() -> None:
    assert spread([]) is None


def test_time_to_minutes_parses_hh_mm_ss() -> None:
    assert _time_to_minutes("06:30:00", default=-1.0) == 390.0
    assert _time_to_minutes("00:00:00", default=-1.0) == 0.0
    assert _time_to_minutes("22:00:00", default=-1.0) == 1320.0


def test_time_to_minutes_falls_back_on_missing_or_malformed() -> None:
    assert _time_to_minutes(None, default=360.0) == 360.0
    assert _time_to_minutes("", default=360.0) == 360.0
    assert _time_to_minutes("not-a-time", default=360.0) == 360.0


def test_config_from_options_empty_is_defaults() -> None:
    assert _config_from_options({}) == ControllerConfig()


def test_config_from_options_overlays_schedule() -> None:
    cfg = _config_from_options(
        {
            CONF_DAY_TEMP: 70.0,
            CONF_NIGHT_TEMP: 64.0,
            CONF_DAY_START: "05:30:00",
            CONF_NIGHT_START: "23:15:00",
            CONF_OPTIMAL_START_LEAD: 30,
        }
    )
    assert cfg.day_temp == 70.0
    assert cfg.night_temp == 64.0
    assert cfg.day_start_min == 330.0  # 5*60 + 30
    assert cfg.night_start_min == 1395.0  # 23*60 + 15
    assert cfg.optimal_start_lead_min == 30
    # Untouched base tunables are preserved.
    assert cfg.deadband == ControllerConfig().deadband


def test_coordinator_reads_schedule_from_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: ["sensor.a"]},
        options={
            CONF_SCHEDULE_ENABLED: True,
            CONF_DAY_TEMP: 70.0,
            CONF_DAY_START: "05:30:00",
        },
    )
    coordinator = MultiroomClimateCoordinator(hass, entry)
    assert coordinator._schedule_enabled is True
    assert coordinator._config.day_temp == 70.0
    assert coordinator._config.day_start_min == 330.0


def _make_coordinator(
    hass: HomeAssistant,
    sensors: list[str],
    entry_id: str = "test_entry",
    humidity_sensor: str | None = None,
    options: dict | None = None,
) -> MultiroomClimateCoordinator:
    data = {CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: sensors}
    if humidity_sensor is not None:
        data[CONF_HUMIDITY_SENSOR] = humidity_sensor
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test", entry_id=entry_id, data=data, options=options or {}
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


# --- humidity sensor wiring (the end-to-end config→read→decide thread) ------

async def test_humidity_sensor_drives_overcool_trim_down(hass: HomeAssistant) -> None:
    # The discriminating test for 6b: a configured RH sensor reading above target, while cooling,
    # must reach decide() and shift the band DOWN (overcool). Same house/band as the settled case
    # below — only the humidity sensor differs — so this proves config→tick→decide is connected.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.rh", "70.0")  # 20 over the 50% default → 2.0°F overcool (the cap)
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"], humidity_sensor="sensor.rh")

    data = await coordinator._async_update_data()

    assert data.humidity == 70.0  # decide() saw the RH the coordinator read
    # effective target 70−2 = 68; error 68−70 = −2; step clamp(0.3*−2, −0.5, 0.5) = −0.5 → band down.
    assert data.proposed is not None
    assert data.proposed.reason == "trim"
    assert data.proposed.set_band is True
    assert data.proposed.band_low == pytest.approx(66.5)
    assert data.proposed.band_high == pytest.approx(68.5)


async def test_no_humidity_sensor_leaves_humidity_none(hass: HomeAssistant) -> None:
    # Without a humidity sensor the same setup is simply settled — overcool never engages.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    data = await coordinator._async_update_data()

    assert data.humidity is None
    assert data.proposed is not None and data.proposed.reason == "within_deadband"


async def test_stale_humidity_disables_overcool(hass: HomeAssistant) -> None:
    # A configured-but-unavailable RH sensor reads as None (no humidity failsafe) → no overcool.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.rh", "unavailable")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"], humidity_sensor="sensor.rh")

    data = await coordinator._async_update_data()

    assert data.humidity is None
    assert data.proposed is not None and data.proposed.reason == "within_deadband"


# --- fan-circulate wiring (spread → decide_fan; shadow only until 6c-2) ------

def _heat_cool_fan(
    low: float, high: float, fan_mode: str, fan_modes: tuple[str, ...] = ("on", "auto")
) -> tuple[str, dict]:
    state, attrs = _heat_cool(low, high)
    return state, {**attrs, "fan_mode": fan_mode, "fan_modes": list(fan_modes)}


async def test_high_spread_proposes_circulate(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")  # spread 6 ≫ high threshold
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data.spread == 6.0
    assert data.fan_proposed.set_fan is True
    assert data.fan_proposed.circulate is True
    assert data.fan_proposed.reason == "spread_high"


async def test_low_spread_returns_fan_to_auto(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.b", "70.2")  # spread 0.2 < low threshold
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "on"))  # already circulating
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data.fan_proposed.set_fan is True
    assert data.fan_proposed.circulate is False
    assert data.fan_proposed.reason == "spread_low"


async def test_single_fresh_sensor_holds_fan(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.b", "unavailable")  # only one fresh → spread None
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    data = await coordinator._async_update_data()

    assert data.spread is None
    assert data.fan_proposed.set_fan is False
    assert data.fan_proposed.reason == "no_spread"


# --- fan write (6c-2: behind the master enable switch) ----------------------

async def test_writes_fan_on_when_enabled_and_spread_high(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")  # spread 6, avg 71 (band settles → no band write)
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])
    coordinator.set_enabled(True)
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "climate.daikin"
    assert calls[0].data["fan_mode"] == "on"
    assert data.fan_blocked is None


async def test_writes_fan_auto_when_enabled_and_spread_low(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.b", "70.2")  # spread 0.2, already circulating → return to auto
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "on"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])
    coordinator.set_enabled(True)
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 1
    assert calls[0].data["fan_mode"] == "auto"
    assert data.fan_blocked is None


async def test_no_fan_write_over_manual_speed(hass: HomeAssistant) -> None:
    # The user has the fan on a manual speed → we don't own it → don't stomp it; surface why.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")  # spread high → would circulate
    hass.states.async_set(
        "climate.daikin",
        *_heat_cool_fan(67.0, 69.0, "low", fan_modes=("low", "medium", "high", "auto", "on")),
    )
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])
    coordinator.set_enabled(True)
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 0
    assert data.fan_blocked == "fan_unmanaged"


async def test_no_fan_write_when_target_mode_unsupported(hass: HomeAssistant) -> None:
    # Equipment has no continuous "on" fan mode → can't circulate; surface why.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto", fan_modes=("auto",)))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])
    coordinator.set_enabled(True)
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 0
    assert data.fan_blocked == "fan_mode_unsupported"


async def test_no_fan_write_when_disabled(hass: HomeAssistant) -> None:
    # Disabled → nothing is written, and it's not "blocked" (just inert); the shadow still shows intent.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])  # default: disabled
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 0
    assert data.fan_blocked is None  # writable, just inert (not blocked) — only disabled
    assert data.fan_proposed.set_fan is True


async def test_fan_block_reason_surfaces_even_when_disabled(hass: HomeAssistant) -> None:
    # Shadow-everything: a circulate that *can't* fire (here, no continuous "on" mode) is diagnosed
    # even with the switch off, so the deferred thresholds are tunable before enabling.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")  # spread high → would circulate
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto", fan_modes=("auto",)))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])  # disabled
    calls = async_mock_service(hass, "climate", "set_fan_mode")

    data = await coordinator._async_update_data()

    assert len(calls) == 0
    assert data.fan_proposed.set_fan is True
    assert data.fan_blocked == "fan_mode_unsupported"


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


async def test_set_target_marks_user_set_and_persists(
    hass: HomeAssistant, hass_storage
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator.async_set_target(72.0)

    assert coordinator._target == 72.0
    assert coordinator._target_user_set is True
    await coordinator._store.async_save(coordinator._persisted_state())
    assert hass_storage[coordinator._store.key]["data"]["target"] == 72.0
    assert hass_storage[coordinator._store.key]["data"]["target_user_set"] is True
    await coordinator.async_shutdown()


async def test_user_target_survives_reenable(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator.async_set_target(72.0)
    coordinator.set_enabled(False)
    coordinator.set_enabled(True)  # user toggle, but the chosen target must be kept

    assert coordinator._target == 72.0
    await coordinator.async_shutdown()


async def test_autoseeded_target_reseeds_on_enable(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # auto-seeds target = 70 (not user-set)
    assert coordinator._target == 70.0
    coordinator.set_enabled(True)  # never user-set → re-seed to "now" on enable

    assert coordinator._target is None


async def test_new_target_feedforwards_and_writes_when_enabled(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])
    coordinator.set_enabled(True)
    await coordinator._async_update_data()  # seed target = 70, last_target = 70

    coordinator._target = 75.0  # a new target diverges from last_target → feedforward next tick
    calls = async_mock_service(hass, "climate", "set_temperature")
    data = await coordinator._async_update_data()

    assert data.proposed is not None and data.proposed.reason == "feedforward"
    assert len(calls) == 1  # the band jumped toward the new target


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


async def _enable_and_drift(hass: HomeAssistant) -> MultiroomClimateCoordinator:
    """Seed a coordinator at target 70 with control enabled, then drift the house cold (→ trim)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])
    coordinator.set_enabled(True)
    await coordinator._async_update_data()  # seeds target = 70 (within deadband, no write)
    hass.states.async_set("sensor.a", "66.0")  # cold → upward trim demanded
    return coordinator


async def test_writes_band_when_enabled(hass: HomeAssistant) -> None:
    coordinator = await _enable_and_drift(hass)
    calls = async_mock_service(hass, "climate", "set_temperature")

    data = await coordinator._async_update_data()

    assert data.proposed is not None and data.proposed.reason == "trim"
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "climate.daikin"
    assert calls[0].data["target_temp_low"] == pytest.approx(67.5)
    assert calls[0].data["target_temp_high"] == pytest.approx(69.5)
    assert coordinator._last_change_ts > 0  # advanced on the successful write


async def test_no_write_when_disabled(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])  # default: disabled
    await coordinator._async_update_data()  # seeds target
    hass.states.async_set("sensor.a", "66.0")
    calls = async_mock_service(hass, "climate", "set_temperature")

    data = await coordinator._async_update_data()

    # decide() still proposes the trim (shadow), but nothing is written and the clock doesn't move.
    assert data.proposed is not None and data.proposed.reason == "trim"
    assert len(calls) == 0
    assert coordinator._last_change_ts == 0.0


async def test_failed_write_does_not_advance_rate_limit(hass: HomeAssistant) -> None:
    coordinator = await _enable_and_drift(hass)

    async def _boom(call: ServiceCall) -> None:
        raise HomeAssistantError("device offline")

    hass.services.async_register("climate", "set_temperature", _boom)

    data = await coordinator._async_update_data()

    assert data.proposed is not None and data.proposed.reason == "trim"
    # A failed write must not advance the rate-limit clock — next tick retries.
    assert coordinator._last_change_ts == 0.0


async def test_no_write_on_failsafe_even_when_enabled(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])
    coordinator.set_enabled(True)
    await coordinator._async_update_data()  # seed
    hass.states.async_set("sensor.a", "unavailable")  # lose sensor → failsafe (set_band False)
    calls = async_mock_service(hass, "climate", "set_temperature")

    data = await coordinator._async_update_data()

    assert data.status == "failsafe"
    assert len(calls) == 0


async def test_failsafe_tick_does_not_save(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    await coordinator._async_update_data()  # tick 1: seeds + learns → a real state change
    hass.states.async_set("sensor.a", "unavailable")  # lose the sensor → failsafe
    with patch.object(coordinator, "_save_state") as save:
        data = await coordinator._async_update_data()  # tick 2: decide runs but nothing mutates

    assert data.status == "failsafe"
    save.assert_not_called()  # snapshot-compare skips the write when no persisted field changed


# --- day/night schedule wiring (7c) ----------------------------------------

_SCHED_OPTS = {
    CONF_SCHEDULE_ENABLED: True,
    CONF_DAY_TEMP: 70.0,
    CONF_NIGHT_TEMP: 64.0,
    CONF_DAY_START: "06:00:00",
    CONF_NIGHT_START: "22:00:00",
    CONF_OPTIMAL_START_LEAD: 0,  # no lead → boundaries land exactly at 06:00 / 22:00
}


def _at(hour: int, minute: int = 0):
    """Patch the coordinator's local clock to a fixed wall-clock time (only hour/minute are read)."""
    return patch(
        "custom_components.multiroom_climate.coordinator.dt_util.now",
        return_value=datetime(2026, 1, 1, hour, minute),
    )


async def test_schedule_jumps_target_at_transition(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"], options=_SCHED_OPTS)

    with _at(21, 0):  # daytime: first tick records the day setpoint and seeds target to the house avg
        data = await coordinator._async_update_data()
    assert data.scheduled == 70.0
    assert data.target == 68.0  # seeded, not jumped (no transition is detectable on the first tick)

    with _at(22, 30):  # crossed into the night period → target jumps to the night setpoint
        data = await coordinator._async_update_data()
    assert data.scheduled == 64.0
    assert data.target == 64.0


async def test_schedule_holds_target_between_transitions(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"], options=_SCHED_OPTS)

    with _at(8, 0):  # day: record setpoint, seed target
        await coordinator._async_update_data()
    coordinator._target = 72.0  # a manual hold mid-period

    with _at(10, 0):  # still day → no transition → the manual hold survives
        data = await coordinator._async_update_data()
    assert data.target == 72.0


async def test_schedule_disabled_leaves_target_alone(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])  # no schedule configured

    with _at(22, 30):
        data = await coordinator._async_update_data()
    assert data.scheduled is None
    assert data.target == 68.0  # normal seed to the house average; the schedule never ran


async def test_schedule_reasserts_after_restart_spanning_transition(hass: HomeAssistant) -> None:
    """The advisor's case: a restart that spans a transition must jump to the new setpoint."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"], options=_SCHED_OPTS)
    # Restored persisted state from before the downtime: the day target + last-seen day setpoint.
    coordinator._target = 70.0
    coordinator._last_target = 70.0
    coordinator._last_scheduled = 70.0

    with _at(22, 30):  # now in the night period → the first tick re-asserts the new setpoint
        data = await coordinator._async_update_data()
    assert data.target == 64.0


def test_last_scheduled_round_trips_through_persisted_state(hass: HomeAssistant) -> None:
    coordinator = _make_coordinator(hass, ["sensor.a"])
    coordinator._last_scheduled = 64.0
    assert coordinator._persisted_state()["last_scheduled"] == 64.0


async def test_failed_fan_write_is_swallowed(hass: HomeAssistant) -> None:
    # A fan-mode write that raises must be logged and swallowed, not break the coordinator update.
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.a", "68.0")
    hass.states.async_set("sensor.b", "74.0")  # spread 6 → circulate
    hass.states.async_set("climate.daikin", *_heat_cool_fan(67.0, 69.0, "auto"))
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])
    coordinator.set_enabled(True)

    calls: list[ServiceCall] = []

    async def _boom(call: ServiceCall) -> None:
        calls.append(call)
        raise HomeAssistantError("fan offline")

    hass.services.async_register("climate", "set_fan_mode", _boom)

    data = await coordinator._async_update_data()  # must not raise

    # The write was actually attempted (not just proposed)...
    assert len(calls) == 1
    assert calls[0].data["fan_mode"] == "on"
    # ...and the raised error was swallowed, so the tick still completed.
    assert data.fan_blocked is None


# --- Once-only "data source unavailable" logging (log_when_unavailable) ---

_COORD_LOGGER = "custom_components.multiroom_climate.coordinator"


def _logs(caplog: pytest.LogCaptureFixture, level: str, needle: str) -> list[str]:
    """Messages at ``level`` whose text contains ``needle`` — used to assert once-only logging."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == level and needle in r.getMessage()
    ]


async def test_thermostat_drop_logs_one_warning_then_recovers_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    coordinator = _make_coordinator(hass, ["sensor.a"])

    with caplog.at_level(logging.INFO, logger=_COORD_LOGGER):
        await coordinator._async_update_data()  # present → silent
        hass.states.async_set("climate.daikin", "unavailable")
        await coordinator._async_update_data()  # drops → one WARNING
        await coordinator._async_update_data()  # still gone → no second WARNING
        hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
        await coordinator._async_update_data()  # returns → one INFO

    assert len(_logs(caplog, "WARNING", "thermostat climate.daikin is unavailable")) == 1
    assert len(_logs(caplog, "INFO", "thermostat climate.daikin is available again")) == 1


async def test_thermostat_startup_appearance_is_silent(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # The wrapped thermostat loads *after* our first poll (startup ordering): absent → present must
    # not log a spurious "unavailable" or "available again".
    hass.states.async_set("sensor.a", "70.0")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    with caplog.at_level(logging.INFO, logger=_COORD_LOGGER):
        await coordinator._async_update_data()  # thermostat absent → silent (never seen present)
        hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
        await coordinator._async_update_data()  # first appearance → silent

    assert _logs(caplog, "WARNING", "thermostat climate.daikin") == []
    assert _logs(caplog, "INFO", "thermostat climate.daikin") == []


async def test_all_sensors_drop_logs_one_warning_then_recovers_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # Thermostat present throughout so only the sensor branch logs.
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    hass.states.async_set("sensor.a", "70.0")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    with caplog.at_level(logging.INFO, logger=_COORD_LOGGER):
        await coordinator._async_update_data()  # fresh → silent
        hass.states.async_set("sensor.a", "unavailable")
        await coordinator._async_update_data()  # all stale → one WARNING
        await coordinator._async_update_data()  # still stale → no second WARNING
        hass.states.async_set("sensor.a", "70.0")
        await coordinator._async_update_data()  # back → one INFO

    assert len(_logs(caplog, "WARNING", "target temperature sensors")) == 1
    assert len(_logs(caplog, "INFO", "target temperature sensor is available again")) == 1


async def test_partial_sensor_loss_does_not_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # Losing *some* (not all) sensors is normal degradation surfaced via fresh/total, not an outage.
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("sensor.b", "71.0")
    coordinator = _make_coordinator(hass, ["sensor.a", "sensor.b"])

    with caplog.at_level(logging.INFO, logger=_COORD_LOGGER):
        await coordinator._async_update_data()
        hass.states.async_set("sensor.b", "unavailable")  # one of two drops
        await coordinator._async_update_data()

    assert _logs(caplog, "WARNING", "target temperature sensors") == []


# --- Repair issue for a removed/missing wrapped thermostat (repair_issues) ---


def _missing_issue(hass: HomeAssistant, coordinator: MultiroomClimateCoordinator):
    return ir.async_get(hass).async_get_issue(DOMAIN, coordinator._missing_issue_id)


async def test_missing_thermostat_raises_repair_only_after_threshold(
    hass: HomeAssistant,
) -> None:
    # Thermostat absent from the state machine (removed/never loaded). The repair must not fire on a
    # brief absence (the startup-ordering race) — only after a sustained run of absent polls.
    hass.states.async_set("sensor.a", "70.0")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    for _ in range(_THERMOSTAT_MISSING_TICKS - 1):
        await coordinator._async_update_data()
    assert _missing_issue(hass, coordinator) is None

    await coordinator._async_update_data()  # crosses the threshold
    assert _missing_issue(hass, coordinator) is not None


async def test_missing_thermostat_repair_clears_when_it_returns(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "70.0")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    for _ in range(_THERMOSTAT_MISSING_TICKS):
        await coordinator._async_update_data()
    assert _missing_issue(hass, coordinator) is not None

    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    await coordinator._async_update_data()
    assert _missing_issue(hass, coordinator) is None


async def test_unavailable_thermostat_raises_no_repair(hass: HomeAssistant) -> None:
    # A registered-but-unavailable thermostat is a transient outage owned by log_when_unavailable,
    # NOT a missing-entity config error — it must never raise the repair, even after a long absence.
    # (This is the discriminator that keeps repair_issues distinct from log_when_unavailable.)
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", "unavailable")
    coordinator = _make_coordinator(hass, ["sensor.a"])

    for _ in range(_THERMOSTAT_MISSING_TICKS + 2):
        await coordinator._async_update_data()

    assert _missing_issue(hass, coordinator) is None


async def test_unload_clears_thermostat_repair_issue(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    # Reconfiguring to a different thermostat goes reconfigure → reload → unload; unloading must clear
    # a standing repair issue so a stale one doesn't linger.
    hass.states.async_set("sensor.a", "70.0")
    hass.states.async_set("climate.daikin", *_heat_cool(67.0, 69.0))
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="climate.daikin",
        data={CONF_CLIMATE_ENTITY: "climate.daikin", CONF_TARGET_SENSORS: ["sensor.a"]},
        title="Test",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue_id = thermostat_missing_issue_id(entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="thermostat_missing",
        translation_placeholders={"entity_id": "climate.daikin"},
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
