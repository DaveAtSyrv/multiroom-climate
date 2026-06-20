"""Unit tests for the pure control engine (no Home Assistant needed)."""

from __future__ import annotations

from dataclasses import replace

from custom_components.multiroom_climate.controller import (
    ControllerConfig,
    ControllerInputs,
    decide,
)

CFG = ControllerConfig(deadband=0.5, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)


def _inputs(**overrides) -> ControllerInputs:
    """A sane default snapshot; override per test. Units are arbitrary (unit-agnostic engine)."""
    base = dict(
        available=True,
        house_average=20.0,
        target=21.0,
        band_low=20.0,
        band_high=23.0,
        now_ts=10_000.0,
        last_change_ts=0.0,  # well past min_period by default
    )
    base.update(overrides)
    return ControllerInputs(**base)


# --- gates -----------------------------------------------------------------

def test_unavailable_triggers_failsafe_and_notifies():
    a = decide(_inputs(available=False), CFG)
    assert a.set_band is False and a.reason == "failsafe" and a.notify


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
    # band_high already at temp_max and we want to go up -> cannot raise without exceeding bound.
    a = decide(_inputs(house_average=10.0, target=30.0, band_low=32.0, band_high=35.0), CFG)
    assert a.set_band is False and a.reason == "at_temp_bound"


def test_can_still_move_down_when_high_at_bound():
    # high at temp_max but we need cooling -> negative step is allowed.
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
    # Larger-magnitude values (F-like) behave the same structurally: too cold -> step up.
    cfg = replace(CFG, max_step=1.0, temp_min=45.0, temp_max=95.0)
    a = decide(_inputs(house_average=66.0, target=70.0, band_low=66.0, band_high=72.0), cfg)
    assert a.set_band is True and a.band_low > 66.0


# --- closed-loop convergence (the merge-blocking test) ---------------------

# A toy *physics* plant, written independently of the controller's formula: the thermostat drives
# its own sensor to mid-band, and the house tracks (mid-band - bias) with first-order lag. `bias`
# stands in for the thermostat's own-sensor offset that the integral controller must absorb.
_CONV_CFG = replace(CFG, deadband=0.2)
_TARGET, _BIAS, _PLANT_GAIN = 21.0, 1.5, 0.4


def _run_plant(feedback_sign: float) -> list[float]:
    """Drive ``decide()`` against the plant for 400 ticks; return the house-temperature history.

    ``feedback_sign`` is +1 to apply the controller's band shift as intended, or -1 to inject a sign
    error (the guard-rail test) — proving the harness actually discriminates direction.
    """
    house = 17.0  # start well below target
    band_low, band_high = 20.0, 23.0
    last_change = 0.0
    t = 0.0
    history: list[float] = []
    for _ in range(400):
        t += _CONV_CFG.min_period_s  # advance past the rate limit each tick
        action = decide(
            ControllerInputs(
                available=True,
                house_average=house,
                target=_TARGET,
                band_low=band_low,
                band_high=band_high,
                now_ts=t,
                last_change_ts=last_change,
            ),
            _CONV_CFG,
        )
        if action.set_band:
            assert action.band_low is not None
            step = (action.band_low - band_low) * feedback_sign
            band_low, band_high = band_low + step, band_high + step
            last_change = t
        mid = (band_low + band_high) / 2.0
        house += _PLANT_GAIN * ((mid - _BIAS) - house)
        history.append(house)
    return history


def test_converges_to_target_absorbing_a_constant_bias():
    """The integral loop reaches the target and stays there, absorbing the constant bias.

    A sign error in ``decide()`` would diverge here where every single-step test still passes.
    """
    history = _run_plant(feedback_sign=1.0)
    assert abs(history[-1] - _TARGET) <= _CONV_CFG.deadband + 1e-6
    assert max(abs(h - _TARGET) for h in history[-20:]) <= _CONV_CFG.deadband + 0.1


def test_a_sign_flip_would_break_convergence():
    """Guard-rail: feeding the negated step back must diverge — so the convergence test isn't trivial."""
    history = _run_plant(feedback_sign=-1.0)
    assert abs(history[-1] - _TARGET) > 1.0
