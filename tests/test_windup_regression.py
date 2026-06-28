"""Closed-loop regression tests for the offset/integral-windup fix.

Encodes the live failure of 2026-06-25/26 (see ``DESIGN_offset_windup.md``) and its fix. Both tests
drive the pure ``decide()`` engine against a **band-coupled saturating plant**: the equipment pulls
the thermostat's own sensor toward the band at a capped rate (so a large gap = saturation), and the
house average tracks the thermostat with a fixed spatial bias (``house = thermostat + bias``; the
true steady-state offset K is therefore ``-bias``).

The coupling matters: a band-*independent* plant would let a degenerate "freeze everything" fix pass,
so these assert both that K never corrupts (no windup) *and* that control still converges (no
over-blocking).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.multiroom_climate.controller import (
    ControllerConfig,
    ControllerInputs,
    decide,
)

CFG = ControllerConfig(
    deadband=0.5,
    kp=0.3,
    max_step=0.5,
    min_period_s=720.0,
    temp_min=7.0,
    temp_max=95.0,
    saturation_margin=2.0,
)

_EQUIP_GAIN = 0.3  # equipment pulls its own sensor toward the band center at this fraction/tick...
_EQUIP_CAP = 0.4  # ...capped here, so a large band-vs-sensor gap means it can't track (saturation)


def _run(
    *,
    target_at: Callable[[int], float],
    k0: float,
    bias: float,
    thermo_start: float,
    band_gap: float,
    n_ticks: int,
    hvac_action: str = "cooling",
) -> list[tuple[float, float, float, float]]:
    """Closed-loop sim. ``decide()`` drives the band; the plant moves the thermostat toward the band
    (saturating) and the house tracks it with ``bias``. Returns per-tick (k, house, band_center, thermo).
    """
    thermo = thermo_start
    house = thermo + bias
    band_center = target_at(0) + k0
    band_low = band_center - band_gap / 2.0
    band_high = band_center + band_gap / 2.0
    k = k0
    last_target: float | None = None  # force an initial feedforward placement
    last_placement_regime: str | None = None
    last_change = 0.0
    ts = 0.0
    history: list[tuple[float, float, float, float]] = []
    for i in range(n_ticks):
        ts += CFG.min_period_s  # always past the rate limit
        target = target_at(i)
        action = decide(
            ControllerInputs(
                available=True,
                house_average=house,
                target=target,
                band_low=band_low,
                band_high=band_high,
                now_ts=ts,
                last_change_ts=last_change,
                # ``hvac_action`` selects the regime in play (the thermostat reports what it's doing).
                # Both offsets seeded to k0 so band placement is well-defined regardless; the action
                # routes which one learns. Default cooling (the saturated-pulldown scenarios); the
                # heating mirror passes hvac_action="heating".
                cool_offset=k,
                heat_offset=k,
                hvac_action=hvac_action,
                last_placement_regime=last_placement_regime,
                last_target=last_target,
                thermostat_temperature=thermo,
            ),
            CFG,
        )
        if action.set_band:
            assert action.band_low is not None and action.band_high is not None
            band_low, band_high = action.band_low, action.band_high
            last_change = ts
        if action.new_offset is not None:
            k = action.new_offset
        if action.placement_regime is not None:
            last_placement_regime = action.placement_regime
        last_target = target
        # Plant: the equipment drives its own sensor toward the band center, at a capped rate — so a
        # large band-vs-sensor gap can't be tracked (that's the saturation the fix keys on). The
        # house average follows the thermostat with a fixed spatial bias.
        center = (band_low + band_high) / 2.0
        step = _EQUIP_GAIN * (center - thermo)
        thermo += max(-_EQUIP_CAP, min(_EQUIP_CAP, step))
        house = thermo + bias
        history.append((k, house, center, thermo))
    return history


def test_setback_pulldown_does_not_corrupt_learned_offset() -> None:
    """The live failure: a day->night setback the AC can't track must not wind K (and must converge)."""
    # Settled at the day target (21) with the true offset K=-2 (house sits 2 above the thermostat),
    # then drop to 18. Without anti-windup the band winds ~8 below steady state and K corrupts to ~-12.
    history = _run(
        target_at=lambda i: 18.0,
        k0=-2.0,
        bias=2.0,
        thermo_start=19.0,  # day steady state: thermo == band_center, house == 21
        band_gap=3.0,
        n_ticks=120,
    )
    ks = [h[0] for h in history]
    final_house = history[-1][1]

    # K never corrupts (the bug drove it past -12)...
    assert min(ks) > -4.0, f"learned_offset corrupted by windup: min={min(ks):.2f}"
    # ...and control is NOT strangled: the house actually reaches the 18 setback target.
    assert final_house == pytest.approx(18.0, abs=0.7), f"did not converge: house={final_house:.2f}"


def test_cold_start_converges_from_a_hot_house() -> None:
    """From K=0 and a hot house, the band must migrate to target+K_true and K learn the true bias.

    This is the over-blocking guard: a fix that froze the band to dodge windup would never converge
    here. Starts deeply cooling-saturated (thermostat far above the band), so it also exercises the
    block -> de-saturate -> ratchet recovery.
    """
    history = _run(
        target_at=lambda i: 21.0,
        k0=0.0,
        bias=2.0,  # true K = -2
        thermo_start=29.0,  # house starts at 31 (10 above target), thermostat flat-out
        band_gap=3.0,
        n_ticks=400,
    )
    final_k, final_house, final_center, _ = history[-1]

    assert final_house == pytest.approx(21.0, abs=1.0), f"house did not converge: {final_house:.2f}"
    assert final_k == pytest.approx(-2.0, abs=0.6), f"K did not learn true bias: {final_k:.2f}"
    assert final_center == pytest.approx(19.0, abs=1.0), f"band did not settle: {final_center:.2f}"


# --- heating-regime convergence (split-offset, the heat-side mirror) --------

# The two windup tests above drive a *cooling* loop (house above target). This is the heating mirror —
# house below a fixed target, the equipment heating up to it, ``hvac_action="heating"`` — proving the
# heat offset converges on the same stable plant. (Per-regime attribution + non-corruption are pinned
# by the controller unit tests; the offset routing is exercised here end to end via ``_run``.)
def test_heating_loop_converges_and_learns_the_heat_offset() -> None:
    history = _run(
        target_at=lambda i: 21.0,
        k0=0.0,
        bias=2.0,  # true K = -2
        thermo_start=13.0,  # house starts at 15 (6 below target), heating from cold
        band_gap=3.0,
        n_ticks=400,
        hvac_action="heating",
    )
    final_k, final_house, final_center, _ = history[-1]

    assert final_house == pytest.approx(21.0, abs=1.0), f"house did not converge: {final_house:.2f}"
    assert final_k == pytest.approx(-2.0, abs=0.6), f"heat offset did not learn: {final_k:.2f}"
    assert final_center == pytest.approx(19.0, abs=1.0), f"band did not settle: {final_center:.2f}"
