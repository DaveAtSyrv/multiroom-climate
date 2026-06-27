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

Learned offset + feedforward — **split by regime (heat vs cool).**
The slow trim above is for *holding*. To move fast we learn the **band-to-house offset** ``K =
band_center - house_average`` at steady state (slow EMA) and *jump* the band so ``band_center =
target + K`` — a feedforward step that recovers in one move instead of crawling there via trim. ``K``
is learned relative to the band (what we actuate), not to the thermostat's own sensor, so it absorbs
both the sensor bias and the band-center-vs-regulation gap in one number and needs no extra sensor.

``K`` is **regime-dependent**: the equipment regulates to a different band edge when heating
(``band_low``) vs cooling (``band_high``), so the band-center-to-house offset differs by roughly the
band gap. We therefore keep **two** learned offsets — ``cool_offset`` and ``heat_offset`` — and:
- **place the band** with the offset for the active *demand* regime (a sticky deadband hysteresis on
  ``error``), jumping on a target change *or* a regime flip (heat↔cool changeover);
- **learn** only the offset for the regime the equipment is *actually* running (``hvac_action``, with
  an ``hvac_mode`` fallback), and only when settled (``|error| <= deadband``) so the house is never
  sampled mid-recovery.
``decide()`` is pure, so it *returns* the new offset (``Action.new_offset`` + ``new_offset_regime``)
and the active ``placement_regime``; the caller persists them. ``new_offset is None`` means "leave
the offsets unchanged" — so a missed return path degrades to "didn't learn", never "wiped K".

Scope boundary: this engine answers "given that we are controlling, what band next?" It deliberately
does **not** own the master enable / kill switch. Whether to call ``decide()`` at all, and the
OFF→manual handback (stop writing and leave the band where it is — already bias-compensated for
current conditions — so the thermostat resumes direct manual control), are the coordinator's
responsibility. ``decide()`` returning ``set_band=False`` only ever means "hold", which is not the
same as the coordinator's handback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Regime = Literal["heat", "cool"]


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``. Caller must ensure ``low <= high``."""
    return max(low, min(high, value))


@dataclass(frozen=True)
class ControllerConfig:
    """Tunable control parameters. All temperatures are in the caller's unit."""

    deadband: float = 0.5
    """No band change while ``abs(error) <= deadband`` (avoids hunting); also the "settled" gate
    for offset learning."""

    regime_flip_margin: float = 1.0
    """How far past target (in the demand direction) the house must sit before the *placement regime*
    flips heat↔cool and fires a changeover feedforward. Deliberately larger than ``deadband`` and
    smaller than the ~2° responsiveness bar: the deadband answers "settled enough to learn", this
    answers "demand has genuinely reversed" — so a transient convergence overshoot can't flip the
    regime and jump the band, while a real seasonal changeover still engages cooling well under +2°."""

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

    saturation_margin: float = 2.0
    """Degrees the wrapped thermostat's *own* sensor must sit beyond the band before the equipment
    counts as saturated (running flat-out and not reaching setpoint). Used to suppress integral
    windup and freeze offset learning while the plant can't track. Validated against live data: a
    modulating system sat ~0.2 deg outside its band, a saturated setback pulldown ~10 deg outside —
    so 2.0 separates them with wide slack both ways. Inactive when no own-sensor reading is supplied."""

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
    """Target held during the day period (caller's unit); set via the schedule options flow."""

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
    """The remote average temperature we regulate."""

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

    cool_offset: float
    """Persisted cooling K = band_center - house_average at a cooling steady state (caller persists
    Action.new_offset under new_offset_regime="cool")."""

    heat_offset: float
    """Persisted heating K, the heating counterpart of ``cool_offset``."""

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

    thermostat_temperature: float | None = None
    """The wrapped thermostat's *own* sensor reading, or ``None`` if unavailable. Lets the controller
    see when the equipment is saturated (its sensor sitting far outside the band = running flat-out
    and not reaching setpoint). ``None`` disables the anti-windup / learning guards — a graceful
    fallback for thermostats that don't expose their own temperature."""

    hvac_action: str | None = None
    """What the equipment is *actually doing now* (HA ``hvac_action``: heating/cooling/drying/idle/...),
    or ``None`` if the wrapped climate doesn't report it. Selects which offset *learns*. Typed ``str``
    (not the HA enum) to keep this engine HA-free; HA's ``HVACAction`` is a StrEnum so it compares equal."""

    hvac_mode: str | None = None
    """The wrapped thermostat's set mode (HA ``hvac_mode``: cool/heat/heat_cool/...). Learning fallback
    when ``hvac_action`` is absent. Typed ``str`` for the same HA-free reason as ``hvac_action``."""

    last_placement_regime: str | None = None
    """The placement regime the caller last acted on; a change vs the freshly computed regime triggers a
    feedforward jump at a heat↔cool changeover (mirrors ``last_target``). ``None`` before the caller has
    ever stored one — which suppresses the flip-jump for that first tick (initial placement is left to
    the normal target-change feedforward + trim)."""


@dataclass(frozen=True)
class Action:
    """What the caller should do this tick. ``set_band`` False means "leave the thermostat alone"."""

    set_band: bool
    band_low: float | None = None
    band_high: float | None = None
    notify: str | None = None
    new_offset: float | None = None
    """Updated learned offset to persist, or None = leave the persisted value unchanged."""
    new_offset_regime: Regime | None = None
    """Which offset ``new_offset`` belongs to (``"cool"``/``"heat"``). ``None`` when not learning."""
    placement_regime: Regime | None = None
    """The demand regime this tick used for band placement, echoed on every non-failsafe return so the
    caller can persist it as next tick's ``last_placement_regime`` (this is what arms the flip gate).
    ``None`` only on the failsafe/unavailable path, where the caller holds the prior value."""
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


def _demand_saturation(inputs: ControllerInputs, config: ControllerConfig) -> int:
    """Sign of equipment saturation: ``-1`` cooling-saturated, ``+1`` heating-saturated, ``0`` not.

    The wrapped thermostat is saturated when its *own* sensor sits beyond the band by more than
    ``saturation_margin`` — it's calling for full output and not reaching setpoint, so further band
    motion in that direction actuates nothing and only winds the integrator. A ``None`` reading (no
    own-sensor signal) reads as ``0``, preserving the pre-guard behaviour.
    """
    thermo = inputs.thermostat_temperature
    if thermo is None:
        return 0
    if thermo > inputs.band_high + config.saturation_margin:
        return -1  # above the cool setpoint and not reaching it — cooling flat-out
    if thermo < inputs.band_low - config.saturation_margin:
        return 1  # below the heat setpoint and not reaching it — heating flat-out
    return 0


# hvac_action / hvac_mode values that map to each regime (lower-cased; HA StrEnums compare as str).
_HEAT_ACTIONS = frozenset({"heating", "preheating", "defrosting"})
_COOL_ACTIONS = frozenset({"cooling", "drying"})


def _learn_regime(inputs: ControllerInputs) -> Regime | None:
    """Which offset to *learn* this tick — the regime the equipment is actually running.

    Keyed on ``hvac_action`` (ground truth for what the plant is doing). When the wrapped climate
    doesn't report an action (idle/fan/off or simply absent), fall back to ``hvac_mode`` so a
    single-mode thermostat still learns; ``heat_cool`` with no action stays ambiguous → ``None`` (hold,
    don't learn) rather than guessing. Returning ``None`` never *stops* control, only learning.
    """
    action = inputs.hvac_action
    if action in _HEAT_ACTIONS:
        return "heat"
    if action in _COOL_ACTIONS:
        return "cool"
    # No decisive action — fall back to the set mode for single-mode thermostats.
    mode = inputs.hvac_mode
    if mode == "cool":
        return "cool"
    if mode == "heat":
        return "heat"
    return None


def _placement_regime(inputs: ControllerInputs, error: float, config: ControllerConfig) -> Regime:
    """Which offset to *place the band* with — the active demand direction, sticky with
    ``regime_flip_margin`` hysteresis so it can't chatter. Flips to ``"cool"`` only when
    ``error < -regime_flip_margin`` (house decisively warmer than the regulation point) and to
    ``"heat"`` only when ``error > +regime_flip_margin``; within the margin it holds the last regime.
    Always defined (defaults ``"cool"`` on the very first tick / a tie).

    The margin (wider than ``deadband``) keeps a transient convergence overshoot from flipping the
    regime and firing a changeover feedforward — only a genuine demand reversal does. Keyed on the
    *same* ``error`` decide() computes (against ``effective_target``), so at the humidity-overcooled
    steady state — house settled a touch below the user's target but still actively cooling —
    ``error ≈ 0`` holds the cool regime instead of flipping to heat.
    """
    if error < -config.regime_flip_margin:
        return "cool"
    if error > config.regime_flip_margin:
        return "heat"
    # Hold the last regime within the margin; default cool on the first tick / a tie / a stray value.
    return "heat" if inputs.last_placement_regime == "heat" else "cool"


def _placement_offset(inputs: ControllerInputs, regime: Regime) -> float:
    """The learned offset for ``regime`` — used to place the band (``band_center = target + offset``)."""
    return inputs.cool_offset if regime == "cool" else inputs.heat_offset


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

    # The active demand regime, computed once and echoed on every (non-failsafe) return so the caller
    # can persist it as next tick's last_placement_regime — that's what arms the changeover flip gate.
    regime = _placement_regime(inputs, error, config)

    def _decided(action: Action) -> Action:
        """Stamp the regime onto an Action (incl. ones built inside _shift_band, which is frozen)."""
        return replace(action, placement_regime=regime)

    # FEEDFORWARD: jump the band so band_center = target + offset(regime), bypassing the deadband +
    # rate-limit gates for a fast recovery. Fires on a target change OR a placement-regime flip (a
    # heat↔cool changeover, where the band must jump to the new regime's offset, not crawl there via
    # trim). The is-not-None guard suppresses a spurious jump on the very first tick (no prior regime
    # to flip from); the regime is still stamped + persisted that tick, arming the gate for next time.
    # Uses the established (pre-update) offset; we never learn on this transient tick.
    regime_flip = inputs.last_placement_regime is not None and regime != inputs.last_placement_regime
    if inputs.target != inputs.last_target or regime_flip:
        shift = (inputs.target + _placement_offset(inputs, regime)) - band_center
        return _decided(_shift_band(inputs, config, shift, "feedforward"))

    if abs(error) <= config.deadband:
        # Settled at target — the ONLY place we learn. Nudge the *active regime's* offset toward
        # (band_center - house_average) with a slow EMA (the baseline is that regime's own current
        # value, so heat and cool don't drag each other). Gating on the deadband means the house is
        # never sampled mid-recovery; the extra saturation gate means we also never sample while the
        # band is still wound away from steady state (a saturated plant), which would corrupt K (see
        # DESIGN_offset_windup.md). Saturation in *either* direction means the in-deadband band isn't a
        # true steady state, so this gate is non-directional (`!= 0`) — unlike the anti-windup trim
        # gate below, which only blocks the *worsening* step. Distinct reasons surface why we held.
        if _demand_saturation(inputs, config) != 0:
            return _decided(Action(set_band=False, reason="within_deadband_saturated"))
        learn_regime = _learn_regime(inputs)
        if learn_regime is None:
            # No decisive regime (idle/fan with no set mode) — hold, don't attribute a sample anywhere.
            return _decided(Action(set_band=False, reason="within_deadband_idle"))
        base = _placement_offset(inputs, learn_regime)
        new_offset = base + config.offset_alpha * ((band_center - inputs.house_average) - base)
        return _decided(
            Action(
                set_band=False,
                reason="within_deadband",
                new_offset=new_offset,
                new_offset_regime=learn_regime,
            )
        )

    if inputs.now_ts - inputs.last_change_ts < config.min_period_s:
        return _decided(Action(set_band=False, reason="rate_limited"))

    # Integral trim: ADD kp*error onto the current band (not an absolute setpoint — see module doc).
    desired_step = _clamp(config.kp * error, -config.max_step, config.max_step)
    # Anti-windup: refuse to push the band further in a saturated demand direction. The equipment is
    # already flat-out, so the step actuates nothing and only winds the band away from steady state
    # (the root of the offset-corruption bug). A step that *relieves* saturation is always allowed.
    saturation = _demand_saturation(inputs, config)
    if (saturation < 0 and desired_step < 0) or (saturation > 0 and desired_step > 0):
        return _decided(Action(set_band=False, reason="windup_blocked"))
    return _decided(_shift_band(inputs, config, desired_step, "trim"))


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
