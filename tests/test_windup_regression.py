"""Regression test for integral/offset windup on a large scheduled setback.

Encodes the live failure of 2026-06-25/26 (see ``DESIGN_offset_windup.md``): on a day->night
setback the velocity-form trim winds the band far below steady state while the house lags (the AC
can't track the pulldown), then ``learned_offset`` (K) learns from that wound-up band once the house
reaches the deadband and corrupts badly. The next feedforward then places the band far too low.

This test drives the *pure* ``decide()`` engine through that scenario and asserts K never corrupts.
It is expected to FAIL until the windup fix lands; the ``xfail(strict=True)`` flips to a hard failure
(XPASS) the moment the fix makes it pass, forcing removal of the marker.
"""

from __future__ import annotations

import pytest

from custom_components.multiroom_climate.controller import (
    ControllerConfig,
    ControllerInputs,
    decide,
)

CFG = ControllerConfig(deadband=0.5, kp=0.3, max_step=0.5, min_period_s=720.0, temp_min=7.0, temp_max=35.0)


def _simulate_setback(
    cfg: ControllerConfig,
    *,
    day_target: float,
    night_target: float,
    k0: float,
    band_gap: float,
    house_cool_per_tick: float,
    n_ticks: int,
) -> list[float]:
    """Run ``decide()`` through a day->night setback with a house that lags the pulldown.

    Starts settled at ``day_target`` with a healthy learned offset ``k0``, fires the setback, and
    threads (band, learned_offset, last_target, last_change) forward across ``n_ticks``. The plant is
    a deliberately slow cooler (``house_cool_per_tick``) representing a saturated AC. Returns the
    learned-offset trajectory so the test can assert it never winds to a wild value.
    """
    band_center = day_target + k0
    band_low = band_center - band_gap / 2.0
    band_high = band_center + band_gap / 2.0
    k = k0
    last_target = day_target
    last_change = 0.0
    house = day_target
    ts = 0.0
    target = night_target  # the setback
    trajectory: list[float] = []
    for _ in range(n_ticks):
        ts += cfg.min_period_s  # always past the rate limit, so trim is never gated by time
        action = decide(
            ControllerInputs(
                available=True,
                house_average=house,
                target=target,
                band_low=band_low,
                band_high=band_high,
                now_ts=ts,
                last_change_ts=last_change,
                learned_offset=k,
                last_target=last_target,
            ),
            cfg,
        )
        if action.set_band:
            assert action.band_low is not None and action.band_high is not None
            band_low, band_high = action.band_low, action.band_high
            last_change = ts
        if action.new_offset is not None:
            k = action.new_offset
        last_target = target
        if house > target:  # slow recovery toward the setback target
            house = max(target, house - house_cool_per_tick)
        trajectory.append(k)
    return trajectory


@pytest.mark.xfail(strict=True, reason="integral/offset windup corrupts K — fixed by the windup PR")
def test_setback_does_not_corrupt_learned_offset() -> None:
    """A saturated setback must not drag the learned offset far from its true value."""
    trajectory = _simulate_setback(
        CFG,
        day_target=21.0,
        night_target=18.0,
        k0=-2.0,
        band_gap=3.0,
        house_cool_per_tick=0.15,
        n_ticks=80,
    )
    worst = min(trajectory)
    # K starts at -2.0 (healthy). It must never wind to a wild magnitude during the setback.
    assert worst > -4.0, f"learned_offset corrupted by windup: min={worst:.2f}"
