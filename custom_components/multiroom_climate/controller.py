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
OFF→manual handback (stop writing and leave the band where it is — already bias-compensated for
current conditions — so the thermostat resumes direct manual control), are the coordinator's
responsibility. ``decide()`` returning ``set_band=False`` only ever means "hold", which is not the
same as the coordinator's handback.
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

    humidity_target: float = 50.0
    """Relative-humidity setpoint (%). Above this, cooling overcools to wring out moisture."""

    humidity_gain: float = 0.1
    """Degrees of overcool per point of RH above ``humidity_target`` (caller's unit)."""

    humidity_max_overcool: float = 2.0
    """Cap on the humidity overcool offset (caller's unit) — bounds how far we chase dryness."""

    fan_spread_high: float = 2.0
    """Room-to-room spread (max−min, caller's unit) at/above which the fan circulates continuously."""

    fan_spread_low: float = 1.0
    """Spread at/below which the fan returns to auto. The gap to ``fan_spread_high`` is the
    hysteresis that prevents the fan thrashing on/off near a single threshold."""

    day_start_min: float = 360.0
    """Day period start, minutes since local midnight (default 06:00). Caller converts wall-clock."""

    night_start_min: float = 1320.0
    """Night (setback) period start, minutes since local midnight (default 22:00)."""

    day_temp: float = 21.0
    """Target held during the day period (caller's unit). Placeholder until a schedule is configured."""

    night_temp: float = 18.0
    """Target held during the night setback (caller's unit)."""

    optimal_start_lead_min: float = 45.0
    """Fixed optimal-start lead: pull each transition this many minutes earlier so the house reaches
    the new setpoint by its scheduled time."""


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

    last_target: float | None
    """The target the caller last acted on; ``target != last_target`` triggers the feedforward jump.
    ``None`` before the caller has ever acted (or right after an explicit target change) — which
    correctly reads as "changed" and fires feedforward."""

    humidity: float | None = None
    """Current relative humidity (%), or ``None`` if no humidity sensor is configured/fresh.
    ``None`` disables overcool entirely."""

    cooling: bool = False
    """Whether the wrapped thermostat is in a cooling-capable mode (COOL/HEAT_COOL). Overcool only
    applies while cooling — gated on the mode, not the house temperature, so our own actuation can't
    flip the gate off and snap back."""


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


def _overcool(inputs: ControllerInputs, config: ControllerConfig) -> float:
    """Degrees to lower the regulation point to wring out humidity, ``0.0`` when not applicable.

    Only when cooling (mode-gated, not temperature-gated — see ``ControllerInputs.cooling``) and a
    fresh humidity reading sits above ``humidity_target``. The offset is ``humidity_gain`` per point
    of excess RH, capped at ``humidity_max_overcool``.
    """
    if inputs.humidity is None or not inputs.cooling:
        return 0.0
    excess = max(0.0, inputs.humidity - config.humidity_target)
    return min(config.humidity_max_overcool, config.humidity_gain * excess)


def decide(inputs: ControllerInputs, config: ControllerConfig) -> Action:
    """Return the next :class:`Action` for one control tick (pure; see module docstring)."""
    # Failsafe: never drive HVAC off a missing or stale reading (and never learn from one).
    if not inputs.available:
        return Action(
            set_band=False,
            notify="Target sensor unavailable — holding setpoint.",
            reason="failsafe",
        )

    # Humidity overcool lowers the whole regulation point symmetrically (not just the cool setpoint):
    # we hold the house a touch below the user's target while it's muggy. Feedforward below stays
    # keyed on the *nominal* target so a target change doesn't re-jump on humidity swings; the slow
    # trim absorbs the overcool bias over subsequent ticks.
    effective_target = inputs.target - _overcool(inputs, config)
    error = effective_target - inputs.house_average
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


@dataclass(frozen=True)
class FanAction:
    """The fan-circulate decision. ``set_fan`` False means "leave the fan mode alone".

    Boolean-only by design — the caller maps ``circulate`` to its platform's fan-mode strings
    (FAN_ON / FAN_AUTO) and checks they're supported. ``circulate`` is meaningful only when
    ``set_fan`` is True: True = run the fan continuously, False = hand it back to auto.
    """

    set_fan: bool
    circulate: bool = False
    reason: str = ""


def decide_fan(spread: float | None, circulating: bool, config: ControllerConfig) -> FanAction:
    """Decide whether to circulate the fan to break up room-to-room stratification (pure).

    Driven purely by the room temperature ``spread`` (max−min across fresh sensors); deliberately
    **not** gated on HVAC mode — circulation matters most when the system is *idle* and there's no
    forced airflow, which is the opposite of the cooling-gated humidity overcool. A two-threshold
    hysteresis band (``fan_spread_high``/``low``) is the only anti-thrash; there is no time limit.

    ``spread is None`` (fewer than two fresh sensors) is the failsafe: hold the fan where it is — the
    stakes are low, so a silent hold beats a notify. ``circulating`` is whether the fan is already
    running continuously, so a change is proposed only when the desired state actually differs.
    """
    if spread is None:
        return FanAction(set_fan=False, reason="no_spread")
    if spread >= config.fan_spread_high:
        desired, reason = True, "spread_high"
    elif spread <= config.fan_spread_low:
        desired, reason = False, "spread_low"
    else:
        return FanAction(set_fan=False, reason="within_hysteresis")
    return FanAction(set_fan=desired != circulating, circulate=desired, reason=reason)


def _in_arc(now: float, start: float, end: float) -> bool:
    """Whether ``now`` lies in the half-open arc ``[start, end)`` on a circular 1440-minute clock.

    ``start <= end`` is the ordinary case; otherwise the arc wraps past midnight (e.g. a night that
    runs 22:00→06:00). Half-open so an exact-boundary ``now == start`` belongs to this arc.
    """
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def scheduled_target(now_minutes: float, config: ControllerConfig) -> float:
    """The day/night setpoint active at ``now_minutes`` (minutes since local midnight), pure.

    Optimal start pulls **both** transitions earlier by ``optimal_start_lead_min`` so the house
    reaches each setpoint by its scheduled time. The night setback therefore visibly begins the lead
    early too — a deliberate v1 simplification (the integration can't know which period is occupied;
    occupancy-aware start is v2). The caller converts HA local wall-clock to minutes and decides
    whether a schedule is configured at all (an unconfigured schedule never calls this).
    """
    lead = config.optimal_start_lead_min
    day_start = (config.day_start_min - lead) % 1440.0
    night_start = (config.night_start_min - lead) % 1440.0
    return config.day_temp if _in_arc(now_minutes, day_start, night_start) else config.night_temp
