"""Unit tests for the pure control engine (no Home Assistant needed)."""

from __future__ import annotations

from collections import namedtuple
from dataclasses import replace

import pytest

from custom_components.multiroom_climate.controller import (
    ControllerConfig,
    ControllerInputs,
    decide,
    decide_fan,
    scheduled_target,
)

CFG = ControllerConfig(deadband=0.5, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)


def _inputs(**overrides) -> ControllerInputs:
    """A sane default snapshot; override per test. Units are arbitrary (unit-agnostic engine).

    ``last_target`` defaults to ``target`` so tests exercise the trim/hold paths without a spurious
    feedforward jump; feedforward tests set ``last_target`` explicitly.
    """
    base = dict(
        available=True,
        house_average=20.0,
        target=21.0,
        band_low=20.0,
        band_high=23.0,
        now_ts=10_000.0,
        last_change_ts=0.0,  # well past min_period by default
        learned_offset=0.0,
    )
    base.update(overrides)
    base.setdefault("last_target", base["target"])
    return ControllerInputs(**base)


# --- gates -----------------------------------------------------------------

def test_unavailable_triggers_failsafe_and_notifies():
    a = decide(_inputs(available=False), CFG)
    assert a.set_band is False and a.reason == "failsafe" and a.notify
    assert a.new_offset is None  # never learn off a stale reading


def test_within_deadband_no_write():
    a = decide(_inputs(house_average=20.8, target=21.0), CFG)  # error 0.2 <= 0.5
    assert a.set_band is False and a.reason == "within_deadband"


def test_rate_limited_no_write():
    a = decide(_inputs(house_average=17.0, now_ts=100.0, last_change_ts=0.0), CFG)  # 100 < 720
    assert a.set_band is False and a.reason == "rate_limited"


# --- direction (sign) ------------------------------------------------------

def test_too_cold_shifts_band_up():
    a = decide(_inputs(house_average=18.0, target=21.0, band_low=20.0, band_high=23.0), CFG)
    assert a.set_band is True
    assert a.band_low > 20.0 and a.band_high > 23.0
    assert (a.band_high - a.band_low) == (23.0 - 20.0)  # gap preserved


def test_too_warm_shifts_band_down():
    a = decide(_inputs(house_average=24.0, target=21.0, band_low=20.0, band_high=23.0), CFG)
    assert a.set_band is True
    assert a.band_low < 20.0 and a.band_high < 23.0
    assert (a.band_high - a.band_low) == (23.0 - 20.0)


# --- magnitude / clamping --------------------------------------------------

def test_step_capped_at_max_step():
    a = decide(_inputs(house_average=0.0, target=30.0), CFG)  # huge error -> would be >max_step
    assert a.band_low - 20.0 == CFG.max_step  # capped at +0.5


def test_step_at_upper_temp_bound_is_blocked():
    a = decide(_inputs(house_average=10.0, target=30.0, band_low=32.0, band_high=35.0), CFG)
    assert a.set_band is False and a.reason == "at_temp_bound"


def test_can_still_move_down_when_high_at_bound():
    a = decide(_inputs(house_average=30.0, target=20.0, band_low=32.0, band_high=35.0), CFG)
    assert a.set_band is True and a.band_high < 35.0


def test_degenerate_band_wider_than_bounds_is_refused():
    cfg = replace(CFG, temp_min=19.0, temp_max=22.0)
    a = decide(_inputs(house_average=18.0, target=21.0, band_low=18.0, band_high=24.0), cfg)
    assert a.set_band is False and a.reason == "band_out_of_bounds"


def test_inverted_band_is_refused():
    a = decide(_inputs(house_average=18.0, target=21.0, band_low=23.0, band_high=20.0), CFG)
    assert a.set_band is False and a.reason == "band_out_of_bounds"


def test_unit_agnostic_same_decision_in_celsius_or_fahrenheit_like_values():
    cfg = replace(CFG, max_step=1.0, temp_min=45.0, temp_max=95.0)
    a = decide(_inputs(house_average=66.0, target=70.0, band_low=66.0, band_high=72.0), cfg)
    assert a.set_band is True and a.band_low > 66.0


# --- feedforward (single step) ---------------------------------------------

def test_feedforward_jumps_band_center_to_target_plus_offset():
    # band center 23, target jumps to 24, learned K=2 -> new center must be 24+2=26 in ONE step.
    a = decide(_inputs(target=24.0, last_target=21.0, learned_offset=2.0, band_low=22.0, band_high=24.0), CFG)
    assert a.set_band is True and a.reason == "feedforward"
    assert (a.band_low + a.band_high) / 2.0 == pytest.approx(24.0 + 2.0)
    assert (a.band_high - a.band_low) == (24.0 - 22.0)  # gap preserved
    assert a.new_offset is None  # no learning on the transient


def test_feedforward_bypasses_deadband():
    # error 0.2 is within deadband, but the target changed -> feedforward still fires.
    a = decide(_inputs(house_average=21.0, target=21.2, last_target=21.0, learned_offset=0.0), CFG)
    assert a.set_band is True and a.reason == "feedforward"


def test_feedforward_bypasses_rate_limit():
    a = decide(_inputs(house_average=21.0, target=24.0, last_target=21.0, now_ts=100.0, last_change_ts=0.0), CFG)
    assert a.set_band is True and a.reason == "feedforward"


def test_feedforward_refused_at_temp_bound_holds():
    # Target beyond equipment range with the band already at the upper bound: the jump clamps to 0
    # and we hold rather than write garbage. (Moot in practice — the target is unreachable — but pinned.)
    a = decide(_inputs(target=40.0, last_target=21.0, band_low=33.0, band_high=35.0), CFG)
    assert a.set_band is False and a.reason == "at_temp_bound"


# --- offset learning -------------------------------------------------------

def test_offset_learned_only_when_settled():
    # within deadband: K eases toward (band_center - house) = (21.5 - 20.8) = 0.7 at alpha 0.05.
    a = decide(_inputs(house_average=20.8, target=21.0, band_low=20.0, band_high=23.0, learned_offset=0.0), CFG)
    assert a.reason == "within_deadband"
    assert a.new_offset == pytest.approx(0.05 * 0.7)


def test_offset_not_learned_on_trim():
    a = decide(_inputs(house_average=18.0, target=21.0), CFG)
    assert a.reason == "trim" and a.new_offset is None


# --- humidity overcool -----------------------------------------------------

# Overcool lowers the *effective* target (the whole regulation point), so a house that was settled
# at the nominal target now reads as too-warm and trims down. Gains here: with humidity_gain 0.1 a
# 20-point RH excess => 2.0° overcool (also the default cap).
_HUMID_CFG = replace(CFG, humidity_target=50.0, humidity_gain=0.1, humidity_max_overcool=2.0)


def test_overcool_trims_down_when_cooling_and_humid():
    # Settled at target (error 0) but RH 70 while cooling -> effective target 21-2=19 -> trim down.
    a = decide(_inputs(house_average=21.0, target=21.0, humidity=70.0, cooling=True), _HUMID_CFG)
    assert a.set_band is True and a.reason == "trim"
    assert a.band_low < 20.0 and a.band_high < 23.0


def test_overcool_offset_capped():
    # RH 200 over -> 0.1*150=15° raw, capped to 2.0: effective target 19, error -2.0 -> trim caps at max_step.
    a = decide(_inputs(house_average=21.0, target=21.0, humidity=200.0, cooling=True), _HUMID_CFG)
    assert a.set_band is True
    assert (20.0 - a.band_low) == _HUMID_CFG.max_step  # error is -2.0, trim clamps to max_step down


def test_no_overcool_when_not_cooling():
    # Same humidity, but heating/off mode: no overcool, so still settled -> hold + learn.
    a = decide(_inputs(house_average=21.0, target=21.0, humidity=70.0, cooling=False), _HUMID_CFG)
    assert a.set_band is False and a.reason == "within_deadband"


def test_no_overcool_when_humidity_none():
    a = decide(_inputs(house_average=21.0, target=21.0, humidity=None, cooling=True), _HUMID_CFG)
    assert a.set_band is False and a.reason == "within_deadband"


def test_no_overcool_when_humidity_at_or_below_target():
    a = decide(_inputs(house_average=21.0, target=21.0, humidity=50.0, cooling=True), _HUMID_CFG)
    assert a.set_band is False and a.reason == "within_deadband"


def test_overcool_does_not_perturb_feedforward():
    # Target change fires feedforward keyed on the NOMINAL target, ignoring the humidity overcool.
    a = decide(
        _inputs(target=24.0, last_target=21.0, learned_offset=2.0, band_low=22.0, band_high=24.0,
                humidity=70.0, cooling=True),
        _HUMID_CFG,
    )
    assert a.reason == "feedforward"
    assert (a.band_low + a.band_high) / 2.0 == pytest.approx(24.0 + 2.0)  # nominal target, not 22


def test_overcool_learns_at_the_overcooled_steady_state():
    # House held at the overcooled point (19 = 21-2): error 0 -> learn. K is target-independent, so
    # learning during overcool is fine (the EMA tracks band_center - house regardless of target).
    a = decide(
        _inputs(house_average=19.0, target=21.0, band_low=20.0, band_high=23.0,
                learned_offset=0.0, humidity=70.0, cooling=True),
        _HUMID_CFG,
    )
    assert a.reason == "within_deadband"
    assert a.new_offset == pytest.approx(0.05 * (21.5 - 19.0))


# --- closed-loop simulation (the merge-blocking tests) ---------------------

# A toy *physics* plant, written independently of the controller's formula: the thermostat drives
# its own sensor to mid-band, and the house tracks (mid-band - bias) with first-order lag. `bias`
# stands in for the offset the loop must learn (with this unit-gain plant, K converges to exactly bias).
_CONV_CFG = replace(CFG, deadband=0.2)
_TARGET, _BIAS, _PLANT_GAIN = 21.0, 1.5, 0.4

_Rec = namedtuple("_Rec", "target house band_center offset reason")


def _simulate(target_per_tick: list[float], use_feedforward: bool, start_house: float = 17.0) -> list[_Rec]:
    """Simulate the coordinator loop tick by tick (persisting band, offset, last_target)."""
    house = start_house
    band_low, band_high = 20.0, 23.0
    learned_offset = 0.0
    last_change = 0.0
    last_target = target_per_tick[0]  # matched at start: no spurious feedforward on tick 0
    t = 0.0
    out: list[_Rec] = []
    for target in target_per_tick:
        t += _CONV_CFG.min_period_s
        action = decide(
            ControllerInputs(
                available=True,
                house_average=house,
                target=target,
                band_low=band_low,
                band_high=band_high,
                now_ts=t,
                last_change_ts=last_change,
                learned_offset=learned_offset,
                last_target=last_target if use_feedforward else target,
            ),
            _CONV_CFG,
        )
        if action.set_band:
            assert action.band_low is not None
            band_low, band_high = action.band_low, action.band_high
            last_change = t
        if action.new_offset is not None:
            learned_offset = action.new_offset
        last_target = target  # coordinator advances last_target each tick
        band_center = (band_low + band_high) / 2.0
        house += _PLANT_GAIN * ((band_center - _BIAS) - house)
        out.append(_Rec(target, house, band_center, learned_offset, action.reason))
    return out


def test_trim_converges_to_target_absorbing_a_constant_bias():
    """Trim alone reaches the target and stays. A sign error in trim would diverge here."""
    out = _simulate([_TARGET] * 400, use_feedforward=False)
    assert abs(out[-1].house - _TARGET) <= _CONV_CFG.deadband + 1e-6
    assert max(abs(r.house - _TARGET) for r in out[-20:]) <= _CONV_CFG.deadband + 0.1


def test_offset_converges_to_the_bias_at_steady_state():
    out = _simulate([_TARGET] * 400, use_feedforward=True)
    assert out[-1].offset == pytest.approx(_BIAS, abs=0.1)  # K learned the plant's bias


def test_feedforward_lands_band_center_in_one_step():
    """The discriminating test: on a target change, band_center jumps to target+K in ONE tick.

    Trim alone would converge to the new target eventually, masking a broken feedforward — so we
    assert the *step*, not the eventual outcome. With the unit-gain plant K==bias, so the jump is exact.
    """
    schedule = [21.0] * 250 + [24.0] * 50
    out = _simulate(schedule, use_feedforward=True)
    switch = next(i for i, r in enumerate(out) if r.target == 24.0)
    ff = out[switch]
    assert ff.reason == "feedforward"
    # band_center == target + learned_offset exactly on the feedforward tick.
    assert ff.band_center == pytest.approx(24.0 + ff.offset)
    assert abs(out[-1].house - 24.0) <= _CONV_CFG.deadband + 0.1  # and the house reaches B


def test_feedforward_recovers_faster_than_trim_only():
    """Feedforward must do real work: reach the new target in materially fewer ticks than trim alone."""
    schedule = [21.0] * 250 + [24.0] * 80

    def ticks_to_recover(use_ff: bool) -> int:
        out = _simulate(schedule, use_feedforward=use_ff)
        switch = next(i for i, r in enumerate(out) if r.target == 24.0)
        for i in range(switch, len(out)):
            if abs(out[i].house - 24.0) <= _CONV_CFG.deadband:
                return i - switch
        return len(out)  # never recovered within the window

    ff = ticks_to_recover(use_ff=True)
    trim_only = ticks_to_recover(use_ff=False)
    assert ff < trim_only  # feedforward is faster
    assert ff * 2 <= trim_only  # and materially so


# --- fan-circulate (pure decide_fan) ---------------------------------------

_FAN_CFG = replace(CFG, fan_spread_high=2.0, fan_spread_low=1.0)


def test_fan_no_spread_holds():
    # Fewer than two fresh sensors → spread is None → hold the fan (the failsafe; no notify).
    a = decide_fan(None, circulating=False, config=_FAN_CFG)
    assert a.set_fan is False and a.reason == "no_spread"


def test_fan_high_spread_starts_circulating():
    a = decide_fan(2.5, circulating=False, config=_FAN_CFG)
    assert a.set_fan is True and a.circulate is True and a.reason == "spread_high"


def test_fan_high_spread_already_on_holds():
    # Above the high threshold but already circulating → no redundant write.
    a = decide_fan(2.5, circulating=True, config=_FAN_CFG)
    assert a.set_fan is False and a.reason == "spread_high"


def test_fan_low_spread_returns_to_auto():
    a = decide_fan(0.5, circulating=True, config=_FAN_CFG)
    assert a.set_fan is True and a.circulate is False and a.reason == "spread_low"


def test_fan_low_spread_already_auto_holds():
    a = decide_fan(0.5, circulating=False, config=_FAN_CFG)
    assert a.set_fan is False and a.reason == "spread_low"


def test_fan_hysteresis_band_holds_either_state():
    # Between low and high: hold whatever the fan is doing (the anti-thrash dead zone).
    assert decide_fan(1.5, circulating=False, config=_FAN_CFG).set_fan is False
    on = decide_fan(1.5, circulating=True, config=_FAN_CFG)
    assert on.set_fan is False and on.reason == "within_hysteresis"


# --- day/night schedule + optimal start (pure scheduled_target) -------------

# Day 06:00 (360) → 22:00 (1320); lead 0 unless a test overrides it. day=70, night=62 (°F-like).
_SCHED_CFG = replace(
    CFG, day_start_min=360.0, night_start_min=1320.0, day_temp=70.0, night_temp=62.0,
    optimal_start_lead_min=0.0,
)

_NOON = 12 * 60.0
_MIDNIGHT_OH_TWO = 2 * 60.0


def test_schedule_day_period_returns_day_temp():
    assert scheduled_target(_NOON, _SCHED_CFG) == 70.0


def test_schedule_night_period_returns_night_temp():
    assert scheduled_target(_MIDNIGHT_OH_TWO, _SCHED_CFG) == 62.0  # 02:00 is in the night arc


def test_schedule_boundary_is_half_open_at_day_start():
    # Exactly at day_start (06:00) → day; one minute before → still night. (Half-open [start, end).)
    assert scheduled_target(360.0, _SCHED_CFG) == 70.0
    assert scheduled_target(359.0, _SCHED_CFG) == 62.0


def test_schedule_boundary_is_half_open_at_night_start():
    assert scheduled_target(1320.0, _SCHED_CFG) == 62.0  # 22:00 → night
    assert scheduled_target(1319.0, _SCHED_CFG) == 70.0  # 21:59 → still day


def test_optimal_start_pulls_day_transition_earlier():
    cfg = replace(_SCHED_CFG, optimal_start_lead_min=45.0)
    # 05:30 (330) is before 06:00 but within the 45-min lead → already conditioning to day_temp.
    assert scheduled_target(330.0, cfg) == 70.0
    # 05:10 (310) is more than 45 min early → still night.
    assert scheduled_target(310.0, cfg) == 62.0


def test_optimal_start_pulls_night_transition_earlier_too():
    # The documented v1 symmetry: the setback also begins the lead early.
    cfg = replace(_SCHED_CFG, optimal_start_lead_min=45.0)
    assert scheduled_target(1290.0, cfg) == 62.0  # 21:30 → already setting back (1320−45=1275)


def test_schedule_degenerate_equal_starts_is_all_night():
    # day_start == night_start → no day window → night_temp everywhere (a sane degenerate result).
    cfg = replace(_SCHED_CFG, day_start_min=480.0, night_start_min=480.0)
    assert scheduled_target(_NOON, cfg) == 62.0
    assert scheduled_target(0.0, cfg) == 62.0


def test_schedule_lead_larger_than_window_resolves_via_modular_arithmetic():
    # A lead wider than a window still resolves cleanly. lead 600: day_start=(360−600)%1440=1200,
    # night_start=(1320−600)%1440=720; at noon (720) the day arc [1200,720) wraps and excludes it.
    cfg = replace(_SCHED_CFG, optimal_start_lead_min=600.0)
    assert scheduled_target(_NOON, cfg) == 62.0


# --- anti-windup + saturation-gated learning (CFG saturation_margin defaults to 2.0) -------------


def test_cooling_saturated_blocks_downward_trim():
    # Thermostat's own sensor well above the cool setpoint -> compressor flat out; a down-step
    # actuates nothing, so it's blocked rather than winding the band below steady state.
    a = decide(
        _inputs(
            house_average=24.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=26.0,  # 26 > 23 + 2 -> cooling-saturated
        ),
        CFG,
    )
    assert a.set_band is False and a.reason == "windup_blocked"


def test_cooling_saturated_still_allows_relieving_upward_trim():
    # Now too cold (wants the band UP) while the cool sensor reads high — that step relieves the
    # saturation, so it must be allowed, not blocked.
    a = decide(
        _inputs(
            house_average=18.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=26.0,
        ),
        CFG,
    )
    assert a.set_band is True and a.band_low > 20.0


def test_heating_saturated_blocks_upward_trim():
    a = decide(
        _inputs(
            house_average=18.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=17.0,  # 17 < 20 - 2 -> heating-saturated; too-cold wants band UP
        ),
        CFG,
    )
    assert a.set_band is False and a.reason == "windup_blocked"


def test_no_thermostat_temp_falls_back_to_unguarded_trim():
    # Graceful degradation: with no own-sensor reading, behave exactly as before (trim proceeds).
    a = decide(
        _inputs(
            house_average=24.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=None,
        ),
        CFG,
    )
    assert a.set_band is True and a.band_low < 20.0


def test_within_deadband_does_not_learn_while_saturated():
    # Settled at target but the compressor is flat out -> the band is not at steady state, so freeze
    # learning (K must not be sampled from a wound-up band).
    a = decide(
        _inputs(
            house_average=21.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=26.0,
        ),
        CFG,
    )
    assert a.reason == "within_deadband_saturated" and a.new_offset is None


def test_within_deadband_learns_when_modulating():
    # Settled and the own sensor sits inside the band (modulating) -> a real steady state, so learn.
    a = decide(
        _inputs(
            house_average=21.0, target=21.0, band_low=20.0, band_high=23.0,
            thermostat_temperature=21.5,
        ),
        CFG,
    )
    assert a.reason == "within_deadband" and a.new_offset is not None
