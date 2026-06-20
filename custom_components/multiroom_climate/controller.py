"""Pure control engine for Multiroom Climate.

``decide()`` is a **pure function** — no Home Assistant calls, no I/O, no clock reads. Given the
current inputs and config it returns an :class:`Action`. This is the unit-test core of the
integration and is **unit-agnostic**: temperatures are in whatever single unit the caller uses
(Option W reads/writes the HA system unit; a future direct-API path is °C). The function does not
care, as long as every value in one call shares one unit.

Control law — **integral (velocity) form, NOT proportional position.**
Each tick we *add* ``kp * error`` onto the current band position and command the shifted band.
Because the adjustment accumulates onto the band the thermostat already holds — rather than
recomputing an absolute setpoint from the error each time — the loop integrates to **zero
steady-state error**. That is precisely what silently absorbs the thermostat's own-sensor bias
(the manual "set 67 to hold 70" trick) without ever modeling it.

Do **not** "simplify" this into a proportional *position* controller
(``band = target ± kp * error``): that reintroduces a permanent steady-state offset and defeats the
whole purpose of the integration. The accumulation is the point.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``. Caller must ensure ``low <= high``."""
    return max(low, min(high, value))


@dataclass(frozen=True)
class ControllerConfig:
    """Tunable control parameters. All temperatures are in the caller's unit."""

    deadband: float = 0.5
    """No band change while ``abs(error) <= deadband`` (avoids hunting)."""

    kp: float = 0.3
    """Integral gain: the band shift added per unit of error, per tick."""

    max_step: float = 0.5
    """Cap on a single tick's band shift (rate limit on magnitude)."""

    min_period_s: float = 720.0
    """Minimum seconds between band changes (rate limit in time). Default 12 min."""

    temp_min: float = 7.0
    """Lower bound the equipment band may not cross (caller's unit; override per system)."""

    temp_max: float = 35.0
    """Upper bound the equipment band may not cross (caller's unit; override per system)."""


@dataclass(frozen=True)
class ControllerInputs:
    """A single snapshot of everything ``decide()`` needs. Caller assembles this from HA state."""

    enabled: bool
    """Master enable. False = the controller proposes no writes (see module/SPEC notes)."""

    available: bool
    """Whether the target sensor(s) are fresh and usable. False triggers the failsafe."""

    house_average: float
    """The remote weighted-average temperature we regulate."""

    target: float
    """The active target, already resolved (day/night selection is the caller's job)."""

    band_low: float
    """Current thermostat AUTO-band low setpoint (heat)."""

    band_high: float
    """Current thermostat AUTO-band high setpoint (cool)."""

    now_ts: float
    """Current time, epoch seconds (passed in so the function stays pure)."""

    last_change_ts: float
    """When the controller last changed the band, epoch seconds."""


@dataclass(frozen=True)
class Action:
    """What the caller should do this tick. ``set_band`` False means "leave the thermostat alone"."""

    set_band: bool
    band_low: float | None = None
    band_high: float | None = None
    notify: str | None = None
    reason: str = ""


def decide(inputs: ControllerInputs, config: ControllerConfig) -> Action:
    """Return the next :class:`Action` for one control tick (pure; see module docstring).

    Gate order is deliberately a flat sequence so PR-#4 feedforward can slot in at the marked
    seam (right after the failsafe) and *bypass* the deadband + rate-limit gates below.
    """
    if not inputs.enabled:
        return Action(set_band=False, reason="disabled")

    # Failsafe: never drive HVAC off a missing or stale reading.
    if not inputs.available:
        return Action(
            set_band=False,
            notify="Target sensor unavailable — holding setpoint.",
            reason="failsafe",
        )

    # --- FEEDFORWARD SEAM (PR #4) ---
    # The jump-on-change (target/schedule change -> command target ± learned_offset immediately)
    # belongs here: after the failsafe, before the gates below, which it must bypass.

    error = inputs.target - inputs.house_average
    if abs(error) <= config.deadband:
        return Action(set_band=False, reason="within_deadband")

    if inputs.now_ts - inputs.last_change_ts < config.min_period_s:
        return Action(set_band=False, reason="rate_limited")

    # Gap-preserving shift: move both band edges by the same step so the heat/cool gap is kept,
    # while neither edge leaves [temp_min, temp_max]. The achievable step interval is
    # [temp_min - band_low, temp_max - band_high]; it is only valid (lo <= hi) while the current
    # band itself fits inside the equipment bounds. Guard the degenerate case so the clamp below
    # can never invert.
    lo_step = config.temp_min - inputs.band_low
    hi_step = config.temp_max - inputs.band_high
    if inputs.band_low > inputs.band_high or lo_step > hi_step:
        return Action(set_band=False, reason="band_out_of_bounds")

    # Integral step: ADD kp*error onto the current band (not an absolute setpoint — see module doc).
    desired_step = _clamp(config.kp * error, -config.max_step, config.max_step)
    effective_step = _clamp(desired_step, lo_step, hi_step)
    if effective_step == 0:
        return Action(set_band=False, reason="at_temp_bound")

    return Action(
        set_band=True,
        band_low=inputs.band_low + effective_step,
        band_high=inputs.band_high + effective_step,
        reason="trim",
    )
