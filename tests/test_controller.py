"""Unit tests for the pure control engine (no Home Assistant needed)."""

from __future__ import annotations

from collections import namedtuple
from dataclasses import replace

import pytest

from custom_components.multiroom_climate.controller import (
    ControllerConfig,
    ControllerInputs,
    decide,
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
