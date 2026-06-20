"""Pure control engine for Multiroom Climate.

``decide()`` is a **pure function** — no Home Assistant calls, no I/O, no clock reads. Given the
current inputs and config it returns an :class:`Action`. This is the unit-test core of the
integration and is **unit-agnostic**: temperatures are in whatever single unit the caller uses
(Option W reads/writes the HA system unit; a future direct-API path is °C). The function does not
care, as long as every value in one call shares one unit.

Control law — **integral (velocity) form, NOT proportional position.**
Each tick we *add* a shift onto the current band position and command the shifted band. Because the
adjustment accumulates onto the band the thermostat already holds — rather than recomputing an
absolute setpoint from the error each time — the loop integrates to **zero steady-state error**.
That is precisely what silently absorbs the thermostat's own-sensor bias (the manual "set 67 to hold
70" trick) without ever modeling it.

Do **not** "simplify" this into a proportional *position* controller
(``band = target ± kp * error``): that reintroduces a permanent steady-state offset and defeats the
whole purpose of the integration. The accumulation is the point.

Learned offset + feedforward.
The slow trim above is for *holding*. To move fast on a target change we learn the **band-to-house
offset** ``K = band_center - house_average`` at steady state (slow EMA), and on any target change we
*jump* the band so ``band_center = target + K`` — a feedforward step that recovers in one move
instead of crawling there via trim. ``K`` is learned relative to the band (what we actuate), not to
the thermostat's own sensor, so it absorbs both the sensor bias and the band-center-vs-regulation
gap in one number and needs no extra sensor. Learning happens **only when settled** (``|error| <=
deadband``) so the house is never sampled mid-recovery. ``decide()`` is pure, so it *returns* the new
offset (``Action.new_offset``); the caller persists it. ``new_offset is None`` means "leave K
unchanged" — so a missed return path degrades to "didn't learn", never "wiped K".

Scope boundary: this engine answers "given that we are controlling, what band next?" It deliberately
does **not** own the master enable / kill switch. Whether to call ``decide()`` at all, and the
OFF→manual handback (restoring the user's setpoint so the thermostat takes over), are the
coordinator's responsibility — ``decide()`` returning ``set_band=False`` only ever means "hold", which
is not the same as "return to manual".
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
    """No band change while ``abs(error) <= deadband`` (avoids hunting); also the "settled" gate
    for offset learning."""

    kp: float = 0.3
    """Integral gain: the band shift added per unit of error, per tick."""

    max_step: float = 0.5
    """Cap on a single trim tick's band shift (feedforward jumps are not capped)."""

    min_period_s: float = 720.0
    """Minimum seconds between *trim* band changes (rate limit in time). Default 12 min."""

    temp_min: float = 7.0
    """Lower bound the equipment band may not cross (caller's unit; override per system)."""

    temp_max: float = 35.0
    """Upper bound the equipment band may not cross (caller's unit; override per system)."""

    offset_alpha: float = 0.05
    """EMA rate for learning the band-to-house offset K (small = slow/robust)."""


@dataclass(frozen=True)
class ControllerInputs:
    """A single snapshot of everything ``decide()`` needs. Caller assembles this from HA state."""

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

    learned_offset: float
    """Persisted K = band_center - house_average at steady state (caller persists Action.new_offset)."""

    last_target: float
    """The target the caller last acted on; ``target != last_target`` triggers the feedforward jump."""


@dataclass(frozen=True)
class Action:
    """What the caller should do this tick. ``set_band`` False means "leave the thermostat alone"."""

    set_band: bool
    band_low: float | None = None
    band_high: float | None = None
    notify: str | None = None
    new_offset: float | None = None
    """Updated learned offset to persist, or None = leave the persisted value unchanged."""
    reason: str = ""


def _shift_band(inputs: ControllerInputs, config: ControllerConfig, shift: float, reason: str) -> Action:
    """Shift both band edges by ``shift``, preserving the heat/cool gap and staying in bounds.

    Shared by feedforward (an absolute jump) and trim (a proportional step). The achievable shift
    interval is ``[temp_min - band_low, temp_max - band_high]``; it is only valid (lo <= hi) while
    the current band itself fits inside the bounds, so guard the degenerate case first. Returns
    ``set_band=False`` with a diagnostic reason when the band is degenerate or already at the bound.
    Carries no ``new_offset`` (neither feedforward nor trim learns).
    """
    lo_step = config.temp_min - inputs.band_low
    hi_step = config.temp_max - inputs.band_high
    if inputs.band_low > inputs.band_high or lo_step > hi_step:
        return Action(set_band=False, reason="band_out_of_bounds")
    effective = _clamp(shift, lo_step, hi_step)
    if effective == 0:
        return Action(set_band=False, reason="at_temp_bound")
    return Action(
        set_band=True,
        band_low=inputs.band_low + effective,
        band_high=inputs.band_high + effective,
        reason=reason,
    )


def decide(inputs: ControllerInputs, config: ControllerConfig) -> Action:
    """Return the next :class:`Action` for one control tick (pure; see module docstring)."""
    # Failsafe: never drive HVAC off a missing or stale reading (and never learn from one).
    if not inputs.available:
        return Action(
            set_band=False,
            notify="Target sensor unavailable — holding setpoint.",
            reason="failsafe",
        )

    error = inputs.target - inputs.house_average
    band_center = (inputs.band_low + inputs.band_high) / 2.0

    # FEEDFORWARD: on a target change, jump the band so band_center = target + learned_offset,
    # bypassing the deadband + rate-limit gates for a fast recovery. Uses the established
    # (pre-update) offset; we never learn on this transient tick.
    if inputs.target != inputs.last_target:
        shift = (inputs.target + inputs.learned_offset) - band_center
        return _shift_band(inputs, config, shift, "feedforward")

    if abs(error) <= config.deadband:
        # Settled at target — the ONLY place we learn. Nudge K toward (band_center - house_average)
        # with a slow EMA. Gating on the deadband means the house is never sampled mid-recovery.
        new_offset = inputs.learned_offset + config.offset_alpha * (
            (band_center - inputs.house_average) - inputs.learned_offset
        )
        return Action(set_band=False, reason="within_deadband", new_offset=new_offset)

    if inputs.now_ts - inputs.last_change_ts < config.min_period_s:
        return Action(set_band=False, reason="rate_limited")

    # Integral trim: ADD kp*error onto the current band (not an absolute setpoint — see module doc).
    desired_step = _clamp(config.kp * error, -config.max_step, config.max_step)
    return _shift_band(inputs, config, desired_step, "trim")
