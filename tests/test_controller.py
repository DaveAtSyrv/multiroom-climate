"""Unit tests for the pure control engine (no Home Assistant needed)."""

from __future__ import annotations

from custom_components.multiroom_climate.controller import (
    Action,
    ControllerConfig,
    ControllerInputs,
    decide,
)


def _inputs(**overrides) -> ControllerInputs:
    """A sane default snapshot; override per test. Units are arbitrary (unit-agnostic engine)."""
    base = dict(
        enabled=True,
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


CFG = ControllerConfig(deadband=0.5, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)


# --- gates -----------------------------------------------------------------

def test_disabled_proposes_no_write():
    a = decide(_inputs(enabled=False), CFG)
    assert a.set_band is False and a.reason == "disabled"


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
    cfg = ControllerConfig(deadband=0.5, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=19.0, temp_max=22.0)
    a = decide(_inputs(house_average=18.0, target=21.0, band_low=18.0, band_high=24.0), cfg)
    assert a.set_band is False and a.reason == "band_out_of_bounds"


def test_inverted_band_is_refused():
    a = decide(_inputs(house_average=18.0, target=21.0, band_low=23.0, band_high=20.0), CFG)
    assert a.set_band is False and a.reason == "band_out_of_bounds"


def test_unit_agnostic_same_decision_in_celsius_or_fahrenheit_like_values():
    # Larger-magnitude values (F-like) behave the same structurally: too cold -> step up.
    cfg = ControllerConfig(deadband=0.5, kp=0.3, max_step=1.0, min_period_s=720.0, temp_min=45.0, temp_max=95.0)
    a = decide(_inputs(house_average=66.0, target=70.0, band_low=66.0, band_high=72.0), cfg)
    assert a.set_band is True and a.band_low > 66.0


# --- closed-loop convergence (the merge-blocking test) ---------------------

def test_converges_to_target_absorbing_a_constant_bias():
    """Drive ``decide()`` against a toy *physics* plant and assert the house reaches the target.

    The plant is written from physics (band up -> house warms), NOT from the controller's formula —
    so a sign error in ``decide()`` makes the loop diverge and this test fails, where every
    single-step test above would still pass. The constant ``bias`` stands in for the thermostat's
    own-sensor offset; the integral controller must absorb it (band settles at ~target+bias).
    """
    cfg = ControllerConfig(deadband=0.2, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)
    target = 21.0
    bias = 1.5          # constant thermostat-vs-house offset the loop must learn out
    plant_gain = 0.4    # first-order approach rate of the house toward its equilibrium

    house = 17.0        # start well below target
    band_low, band_high = 20.0, 23.0
    last_change = 0.0
    t = 0.0
    history: list[float] = []

    for _ in range(400):
        t += cfg.min_period_s  # advance past the rate limit each tick
        action = decide(
            ControllerInputs(
                enabled=True,
                available=True,
                house_average=house,
                target=target,
                band_low=band_low,
                band_high=band_high,
                now_ts=t,
                last_change_ts=last_change,
            ),
            cfg,
        )
        if action.set_band:
            assert action.band_low is not None and action.band_high is not None
            band_low, band_high = action.band_low, action.band_high
            last_change = t

        # PHYSICS plant (independent of the controller): the thermostat drives its own sensor to
        # mid-band; the house tracks (mid-band - bias) with first-order lag.
        mid = (band_low + band_high) / 2.0
        house += plant_gain * ((mid - bias) - house)
        history.append(house)

    # Converged to target, and stayed there (no oscillation/divergence).
    assert abs(history[-1] - target) <= cfg.deadband + 1e-6
    assert max(abs(h - target) for h in history[-20:]) <= cfg.deadband + 0.1


def test_a_sign_flip_would_break_convergence():
    """Guard-rail: confirm the convergence harness actually discriminates direction.

    Re-runs the same plant but feeds the *negated* step back (simulating a sign error). It must
    diverge — proving the convergence test above isn't trivially satisfiable.
    """
    cfg = ControllerConfig(deadband=0.2, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)
    target, bias, plant_gain = 21.0, 1.5, 0.4
    house = 17.0
    band_low, band_high = 20.0, 23.0
    last_change = 0.0
    t = 0.0
    for _ in range(400):
        t += cfg.min_period_s
        action = decide(
            ControllerInputs(True, True, house, target, band_low, band_high, t, last_change), cfg
        )
        if action.set_band:
            step = action.band_low - band_low
            band_low, band_high = band_low - step, band_high - step  # NEGATED (inject a sign error)
            last_change = t
        mid = (band_low + band_high) / 2.0
        house += plant_gain * ((mid - bias) - house)

    assert abs(house - target) > 1.0  # diverged, as a wrong sign should
