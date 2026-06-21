"""Coordinator: poll the target sensors + wrapped thermostat and run the controller each tick.

Per SPEC §5 the coordinator owns *all* the reads each tick — the room sensors (for the house
average) and the wrapped thermostat (its HVAC mode, AUTO band, and temperature bounds). It
runs the pure ``controller.decide()`` each tick and records the proposed ``Action``. When the master
switch is **on** it writes that band to the thermostat via ``climate.set_temperature``; when **off**
it only records the proposal (so the ``shadow_*`` attributes still show what it would do). Control
state is persisted to a ``Store`` so the learned bias survives restarts.

Sensor availability + failsafe (SPEC §3/§4.8): a fresh average needs at least one usable
sensor. With *some* sensors stale we still regulate off the survivors (a transient dropout must not
freeze the HVAC) and expose ``fresh/total`` so the degradation is visible. With *no* fresh sensor we
hand ``available=False`` to ``decide()`` — but only once we were already regulating — so it returns
the failsafe (freeze + a would-notify message); before the first-ever reading we simply wait.

Entities render ``CoordinatorData``; they never reach around the coordinator to read state directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import logging

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    FAN_AUTO,
    FAN_ON,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import convert
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DAY_START,
    CONF_DAY_TEMP,
    CONF_HUMIDITY_SENSOR,
    CONF_NIGHT_START,
    CONF_NIGHT_TEMP,
    CONF_OPTIMAL_START_LEAD,
    CONF_SCHEDULE_ENABLED,
    CONF_TARGET_SENSORS,
    DOMAIN,
)
from .controller import (
    Action,
    ControllerConfig,
    ControllerInputs,
    FanAction,
    decide,
    decide_fan,
    scheduled_target,
)

_LOGGER = logging.getLogger(__name__)
_UPDATE_INTERVAL = timedelta(seconds=60)


def _time_to_minutes(value: str | None, default: float) -> float:
    """Convert a ``"HH:MM:SS"`` wall-clock string (the TimeSelector's format) to minutes-since-midnight.

    Falls back to ``default`` for a missing/malformed value so a half-written option can't crash the
    tick. The engine speaks minutes (see ``scheduled_target``); this is the HA-boundary translation.
    """
    if not value:
        return default
    try:
        hh, mm, *_ = value.split(":")
        return float(int(hh) * 60 + int(mm))
    except (ValueError, TypeError):
        return default


def _config_from_options(options: dict) -> ControllerConfig:
    """Overlay any configured day/night schedule on the base controller config.

    Empty options (schedule never configured) fall through to plain ``ControllerConfig()`` defaults via
    the per-field ``.get(..., default)`` fallbacks.
    """
    base = ControllerConfig()
    return replace(
        base,
        day_temp=options.get(CONF_DAY_TEMP, base.day_temp),
        night_temp=options.get(CONF_NIGHT_TEMP, base.night_temp),
        day_start_min=_time_to_minutes(options.get(CONF_DAY_START), base.day_start_min),
        night_start_min=_time_to_minutes(options.get(CONF_NIGHT_START), base.night_start_min),
        optimal_start_lead_min=options.get(CONF_OPTIMAL_START_LEAD, base.optimal_start_lead_min),
    )

# Status strings for the cases where decide() doesn't run (it owns its own reasons otherwise).
_STATUS_NO_BAND = "no_thermostat_band"
_STATUS_WAITING = "waiting_for_first_reading"

# Persistence: the learned offset converges via a slow EMA, so it's expensive to relearn after a
# restart. Debounced writes batch the per-tick offset nudges to roughly one disk write per delay.
_STORE_VERSION = 1
_SAVE_DELAY_S = 600.0


def build_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, float | bool | None]]:
    """The per-config-entry Store holding the coordinator's control state.

    A module-level factory so entry removal can delete the file without constructing a coordinator.
    """
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One virtual SERVICE device per entry, so the climate + switch entities group together in the UI.

    SERVICE because there's no physical device of our own — we drive an existing thermostat.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Multiroom Climate",
        entry_type=DeviceEntryType.SERVICE,
    )


def house_average(temps: list[float]) -> float | None:
    """Equal-weight mean of the valid sensor temperatures, or None if there are none.

    Per-sensor weights are a v2 feature; v1 weights every sensor equally.
    """
    return sum(temps) / len(temps) if temps else None


def spread(temps: list[float]) -> float | None:
    """Room-to-room temperature spread (max−min), or None with fewer than two fresh sensors.

    The fan-circulate signal: a large spread means the house is stratified and the fan should run.
    ``None`` (a single-sensor install, or only one fresh sensor) simply means "can't tell" — the fan
    holds.
    """
    return max(temps) - min(temps) if len(temps) >= 2 else None


# Fan-circulate only manages the on↔auto pair; a manual speed (low/medium/…) or an unreadable mode is
# left untouched (we don't own it). The single source for the circulate-bool → fan-mode-string mapping.
_MANAGED_FAN_MODES = (FAN_ON, FAN_AUTO)


def fan_mode_for(circulate: bool) -> str:
    """Map the controller's ``circulate`` bool to the wrapped thermostat's fan-mode string."""
    return FAN_ON if circulate else FAN_AUTO


def _to_hvac_mode(value: str) -> HVACMode | None:
    """Coerce a state/attribute string to an HVACMode, or None if it isn't one."""
    try:
        return HVACMode(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class _WrappedReading:
    """One read of the wrapped thermostat: its mode, advertised modes, AUTO band, and temp bounds.

    Every field is ``None`` (or empty) when the thermostat is missing/unavailable or — for the band
    and bounds — when the current mode doesn't advertise them.
    """

    hvac_mode: HVACMode | None
    hvac_modes: tuple[HVACMode, ...]
    band_low: float | None
    band_high: float | None
    temp_min: float | None
    temp_max: float | None
    target_temp_step: float | None
    fan_mode: str | None
    fan_modes: tuple[str, ...]

    @property
    def present(self) -> bool:
        """Whether the wrapped thermostat exists in a known HVAC mode (vs missing/unavailable)."""
        return self.hvac_mode is not None

    @property
    def has_band_and_bounds(self) -> bool:
        """Whether decide() can run: a full AUTO band plus the equipment's temp bounds to clamp to."""
        return None not in (self.band_low, self.band_high, self.temp_min, self.temp_max)

    @property
    def is_cooling(self) -> bool:
        """Whether the current mode is cooling-capable (COOL/HEAT_COOL) — gates humidity overcool."""
        return self.hvac_mode in (HVACMode.COOL, HVACMode.HEAT_COOL)

    @property
    def is_circulating(self) -> bool:
        """Whether the fan is already running continuously (FAN_ON) vs auto/unknown."""
        return self.fan_mode == FAN_ON


# The "thermostat missing/unavailable" reading — shareable because the dataclass is frozen.
_NO_READING = _WrappedReading(
    hvac_mode=None,
    hvac_modes=(),
    band_low=None,
    band_high=None,
    temp_min=None,
    temp_max=None,
    target_temp_step=None,
    fan_mode=None,
    fan_modes=(),
)


@dataclass
class _Availability:
    """Once-only availability bookkeeping for one data source (the thermostat, or the sensor set).

    ``present`` is the source's last-seen state; ``drop_logged`` records whether its current outage
    has already produced a WARNING. Seeded *absent and unlogged* so a source that merely *appears*
    (startup ordering — the wrapped entity loads after our first poll) is silent: only a genuine
    present→absent transition logs. See ``_log_availability``.
    """

    present: bool = False
    drop_logged: bool = False


def _log_availability(
    state: _Availability, *, available: bool, on_lost: str, on_back: str
) -> None:
    """Log a data source dropping (WARNING) and returning (INFO) exactly once each.

    ``state`` is seeded absent-but-unlogged, and the recovery log is gated on having logged the
    drop — so a source that only *appears* (loads after our first poll) is silent, and a genuine
    present→absent→present cycle logs one WARNING then one INFO. A source that's configured but
    never once present is a persistent config problem (a repair issue), not a transient drop, so
    it is intentionally not reported here. Pre-formatted messages keep the call sites readable;
    these events are rare, so eager formatting costs nothing.
    """
    if available and not state.present:
        if state.drop_logged:
            _LOGGER.info(on_back)
            state.drop_logged = False
        state.present = True
    elif not available and state.present:
        _LOGGER.warning(on_lost)
        state.drop_logged = True
        state.present = False


@dataclass(frozen=True)
class CoordinatorData:
    """The regulated view computed each tick: the house average + the wrapped thermostat's state.

    ``band_low``/``band_high`` are the wrapped thermostat's current AUTO setpoints — the band the
    controller slides. ``target``/``learned_offset``/``proposed`` are the controller's decision this
    tick (the band in ``proposed`` is written to the thermostat when ``enabled``, else just recorded).
    ``status`` is the one-word reason for this tick (``decide()``'s reason, or why it didn't run);
    ``fresh_sensors``/``total_sensors`` expose sensor degradation; ``thermostat_present`` drives
    entity availability; ``enabled`` is the master-switch state.
    """

    house_average: float | None
    hvac_mode: HVACMode | None
    hvac_modes: tuple[HVACMode, ...]
    band_low: float | None
    band_high: float | None
    temp_min: float | None  # wrapped thermostat's lower bound — bounds the house-target dial
    temp_max: float | None  # wrapped thermostat's upper bound — bounds the house-target dial
    target_temp_step: float | None  # wrapped thermostat's step (None = HA default) for the house-target dial
    target: float | None
    scheduled: float | None  # day/night setpoint for now (None = no schedule); drives the target at transitions
    learned_offset: float
    humidity: float | None  # the RH decide() saw this tick (None = no sensor/stale → overcool off)
    spread: float | None  # room-to-room max−min (None = <2 fresh sensors); drives fan-circulate
    fan_proposed: FanAction  # the fan-circulate decision
    fan_blocked: str | None  # why an enabled circulate didn't write (None = wrote or nothing to do)
    proposed: Action | None
    status: str
    fresh_sensors: int
    total_sensors: int
    thermostat_present: bool
    enabled: bool


class MultiroomClimateCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Polls sensors + wrapped thermostat, runs ``decide()`` (actuating when enabled), exposes a ``CoordinatorData``."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=_UPDATE_INTERVAL,
        )
        self._sensors: list[str] = entry.data[CONF_TARGET_SENSORS]
        self._wrapped: str = entry.data[CONF_CLIMATE_ENTITY]
        # Optional RH sensor; .get() so entries created before this key still load. None disables overcool.
        self._humidity_sensor: str | None = entry.data.get(CONF_HUMIDITY_SENSOR)

        # Base tunables (deadband, gain, rate limit, EMA) plus any day/night schedule from the options
        # flow. The safety bounds (temp_min/temp_max) are overridden each tick from the wrapped
        # thermostat's own min_temp/max_temp — already in the system unit and correct for the actual
        # equipment — so the defaults here are just a base.
        self._config = _config_from_options(entry.options)
        # Day/night schedule: the gate, plus the last scheduled setpoint seen (so we re-assert the
        # target only when it *changes* — see _async_update_data). Persisted so a restart spanning a
        # transition still picks up the new setpoint instead of holding the old one until the next one.
        self._schedule_enabled: bool = entry.options.get(CONF_SCHEDULE_ENABLED, False)

        # Control state — restored from disk in async_load_state(), then kept in memory and saved
        # back (debounced) as it evolves. Persisting it means the learned bias and target survive
        # restarts instead of relearning from scratch.
        self._store = build_store(hass, entry)
        self._target: float | None = None
        self._last_target: float | None = None
        self._learned_offset: float = 0.0
        self._last_change_ts: float = 0.0
        self._last_scheduled: float | None = None
        # Whether the user explicitly set the target (via async_set_target) vs auto-seeded. An
        # explicit target is kept across an enable toggle; an auto-seeded one is re-seeded to "now"
        # (see set_enabled). Schedule transitions set the target directly and leave this False on
        # purpose, so the re-enable re-seed still works (see _apply_schedule).
        self._target_user_set: bool = False

        # Master enable (the kill switch). Default off: a fresh install is inert until the user opts
        # in, so it never drives the thermostat unexpectedly. Owned here; the switch entity sets it.
        self.enabled: bool = False

        # Once-only "data source unavailable" logging (log_when_unavailable): one WARNING when the
        # thermostat — or every target sensor — drops, one INFO when it returns, instead of spamming
        # the log every 60s poll. Both seeded absent/unlogged so a startup-ordering appearance is
        # silent (see _Availability / _log_availability).
        self._thermostat_avail = _Availability()
        self._sensors_avail = _Availability()

    async def async_load_state(self) -> None:
        """Restore persisted control state before the first refresh.

        Restoring ``target`` means we don't re-seed it to the current house average on restart.
        Restoring ``last_target`` (which a user target change deliberately leaves behind, so a
        feedforward can be pending) keeps the feedforward gate sound across the restart.
        """
        stored = await self._store.async_load()
        if not stored:
            return
        self._learned_offset = stored.get("learned_offset", 0.0)
        self._target = stored.get("target")
        self._last_target = stored.get("last_target")
        self._last_change_ts = stored.get("last_change_ts", 0.0)
        self._target_user_set = stored.get("target_user_set", False)
        self._last_scheduled = stored.get("last_scheduled")

    @callback
    def _persisted_state(self) -> dict[str, float | bool | None]:
        return {
            "learned_offset": self._learned_offset,
            "target": self._target,
            "last_target": self._last_target,
            "last_change_ts": self._last_change_ts,
            "target_user_set": self._target_user_set,
            "last_scheduled": self._last_scheduled,
        }

    def _save_state(self) -> None:
        """Debounced write of the control state to disk (the ``.storage`` file, not the thermostat)."""
        self._store.async_delay_save(self._persisted_state, _SAVE_DELAY_S)

    @callback
    def set_enabled(self, enabled: bool, *, reseed: bool = True) -> None:
        """Flip the kill switch (the switch entity calls this).

        A user toggle (``reseed=True``) re-seeds an *auto-seeded* target to the current house average
        on enable — so turning control on means "hold where we are now" rather than jumping toward a
        possibly-stale seed — while keeping the expensive learned offset. An explicitly-set target is
        kept. Restore-on-restart passes ``reseed=False`` to resume the persisted target as-is.
        Disabling just stops writing; the band is left as-is (already bias-compensated for current
        conditions), so the handback is clean.
        """
        self.enabled = enabled
        # Only auto-seeded targets get re-seeded to "now"; a user's chosen target is kept.
        if enabled and reseed and not self._target_user_set:
            self._target = None
            self._last_target = None

    async def async_set_target(self, target: float) -> None:
        """Set the user's desired house temperature (the climate entity calls this).

        ``_last_target`` is deliberately left unchanged so the next tick sees ``target !=
        last_target`` and feedforward-jumps the band to the new target in one move.
        """
        self._target = target
        self._target_user_set = True
        self._save_state()
        await self.async_request_refresh()

    def _apply_schedule(self) -> float | None:
        """Re-assert the target on a day/night schedule transition; return the scheduled setpoint.

        Returns ``None`` when no schedule is configured. Otherwise it computes the setpoint for the
        current *local* wall-clock (never UTC — the schedule is in local time) and, on a *change* vs
        the last tick, jumps the target to it. Between transitions the target is left alone, so a
        manual hold — or the re-seed after an enable toggle — survives until the next transition.
        Enabling a schedule mid-period is therefore inert until that period ends; the first visible
        change is the next transition. Runs every tick like ``decide()``; the resulting write is still
        gated on the kill switch (the seed at ``_run_decide`` already mutates the target while
        disabled, so this is consistent).
        """
        if not self._schedule_enabled:
            return None
        now = dt_util.now()  # local time → minutes-since-local-midnight, the unit scheduled_target wants
        scheduled = scheduled_target(now.hour * 60 + now.minute, self._config)
        if self._last_scheduled is not None and scheduled != self._last_scheduled:
            # A transition. Jump the target; leave _last_target so the next decide() feedforward-jumps
            # the band. Deliberately NOT setting _target_user_set — a schedule target stays auto-seeded
            # so an enable toggle re-seeds to "now" (the re-enable handback relies on this; see
            # set_enabled). Marking it user-set here would silently break that.
            self._target = scheduled
        self._last_scheduled = scheduled
        return scheduled

    def _read_state_float(self, entity_id: str) -> float | None:
        """Read one entity's numeric state, or ``None`` if missing/unavailable/non-numeric."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return convert(state.state, float)

    def _read_sensors(self) -> list[float]:
        """Return the fresh room temperatures (skipping unavailable/unknown/non-numeric).

        The caller derives the house average, fresh count, and spread from this one read.
        """
        return [v for s in self._sensors if (v := self._read_state_float(s)) is not None]

    def _read_humidity(self) -> float | None:
        """Current relative humidity, or ``None`` if no sensor is configured/fresh.

        ``None`` simply disables overcool (humidity is a comfort feature, not safety-critical — there
        is no humidity failsafe; the temperature staleness policy is the only one that freezes HVAC).
        """
        if self._humidity_sensor is None:
            return None
        return self._read_state_float(self._humidity_sensor)

    def _read_wrapped(self) -> _WrappedReading:
        wrapped = self.hass.states.get(self._wrapped)
        if wrapped is None:
            return _NO_READING
        hvac_mode = (
            _to_hvac_mode(wrapped.state)
            if wrapped.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else None
        )
        hvac_modes = tuple(
            mode
            for mode in (_to_hvac_mode(v) for v in wrapped.attributes.get("hvac_modes", []))
            if mode is not None
        )
        return _WrappedReading(
            hvac_mode=hvac_mode,
            hvac_modes=hvac_modes,
            band_low=convert(wrapped.attributes.get(ATTR_TARGET_TEMP_LOW), float),
            band_high=convert(wrapped.attributes.get(ATTR_TARGET_TEMP_HIGH), float),
            temp_min=convert(wrapped.attributes.get(ATTR_MIN_TEMP), float),
            temp_max=convert(wrapped.attributes.get(ATTR_MAX_TEMP), float),
            target_temp_step=convert(wrapped.attributes.get(ATTR_TARGET_TEMP_STEP), float),
            fan_mode=wrapped.attributes.get(ATTR_FAN_MODE),
            fan_modes=tuple(wrapped.attributes.get(ATTR_FAN_MODES, [])),
        )

    def _evaluate(
        self, house_avg: float | None, wrapped: _WrappedReading, humidity: float | None
    ) -> tuple[Action | None, str, float | None]:
        """Decide what (if anything) to propose this tick. Returns (action, status, tick timestamp).

        Three cases once the thermostat advertises a band + bounds (else we can't act at all):
        - a fresh house average → seed-if-needed and run the normal control tick;
        - no fresh average but we were already regulating → ``decide(available=False)`` failsafe;
        - no fresh average and never seeded → wait (skip *before* building ``ControllerInputs`` so a
          ``None`` target can't reach the dataclass).

        The timestamp is returned so the caller can stamp ``last_change_ts`` with the same tick time
        *after* a successful write; it's ``None`` when ``decide()`` didn't run.
        """
        if not wrapped.has_band_and_bounds:
            return None, _STATUS_NO_BAND, None
        if house_avg is not None:
            action, now_ts = self._run_decide(house_avg, wrapped, humidity, available=True)
            return action, action.reason, now_ts
        if self._target is not None:
            # Was regulating, then lost every sensor → failsafe (house_average is a don't-care here).
            action, now_ts = self._run_decide(self._target, wrapped, humidity, available=False)
            return action, action.reason, now_ts
        return None, _STATUS_WAITING, None

    def _run_decide(
        self, house_avg: float, wrapped: _WrappedReading, humidity: float | None, *, available: bool
    ) -> tuple[Action, float]:
        """Run ``decide()`` and advance the *learning* state (offset, target). Returns the action and
        the tick timestamp.

        It deliberately does **not** stamp ``last_change_ts`` or perform the write — those happen in
        the async path only on a *successful* write, because the rate limit must track real band
        changes, not proposals. (Stamping here would let a disabled stretch's proposals phantom-rate-
        limit the first real write.) Safety bounds come from the wrapped thermostat's min/max.

        When ``available`` is False (lost all sensors mid-regulation) ``decide()`` short-circuits to
        the failsafe: freeze + a would-notify message, no learning — so ``house_avg`` is unused and
        the caller passes the retained target as a placeholder.
        """
        # Seed the target to the current operating point so the within-deadband learning branch runs
        # from the first tick (enabling control re-seeds the target the same way).
        if available and self._target is None:
            self._target = house_avg
            self._last_target = house_avg

        config = replace(self._config, temp_min=wrapped.temp_min, temp_max=wrapped.temp_max)
        now_ts = dt_util.utcnow().timestamp()
        action = decide(
            ControllerInputs(
                available=available,
                house_average=house_avg,
                target=self._target,
                band_low=wrapped.band_low,
                band_high=wrapped.band_high,
                now_ts=now_ts,
                last_change_ts=self._last_change_ts,
                learned_offset=self._learned_offset,
                last_target=self._last_target,
                # Humidity overcool: mode-gated cooling flag + the RH read (None when no sensor is
                # configured/fresh, which simply disables overcool). decide() ignores both on the
                # failsafe path, so passing them when available=False is harmless.
                humidity=humidity,
                cooling=wrapped.is_cooling,
            ),
            config,
        )
        if action.new_offset is not None:
            self._learned_offset = action.new_offset
        self._last_target = self._target
        return action, now_ts

    async def _write_band(self, action: Action) -> bool:
        """Push the proposed band to the wrapped thermostat. Returns True iff the write succeeded.

        A failed write is logged and swallowed so it can't break the coordinator update or advance
        ``last_change_ts``; we re-read the real band next tick, so the model self-corrects.
        """
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self._wrapped,
                    ATTR_TARGET_TEMP_LOW: action.band_low,
                    ATTR_TARGET_TEMP_HIGH: action.band_high,
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - a failed write must not break the update
            _LOGGER.warning("Failed to set %s band: %s", self._wrapped, err)
            return False
        return True

    def _fan_block_reason(self, wrapped: _WrappedReading, action: FanAction) -> str | None:
        """Why an intended fan change can't be written, or ``None`` if it can.

        We only manage the on/auto pair: a manual speed (low/medium/…) or an unreadable mode is left
        untouched (we don't own it), and the target mode must be one the equipment advertises. These
        are surfaced (not silently dropped) so a circulate that *wanted* to fire but couldn't is
        diagnosable during live tuning — e.g. if the equipment's idle fan isn't literally ``auto``.
        """
        if wrapped.fan_mode not in _MANAGED_FAN_MODES:
            return "fan_unmanaged"
        if fan_mode_for(action.circulate) not in wrapped.fan_modes:
            return "fan_mode_unsupported"
        return None

    async def _write_fan(self, action: FanAction) -> None:
        """Set the wrapped thermostat's fan mode. No rate limit — ``decide_fan``'s hysteresis and the
        ``desired != current`` guard already prevent thrash. A failed write is logged and swallowed;
        we re-read the real fan mode next tick, so the model self-corrects.
        """
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_FAN_MODE,
                {ATTR_ENTITY_ID: self._wrapped, ATTR_FAN_MODE: fan_mode_for(action.circulate)},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - a failed write must not break the update
            _LOGGER.warning("Failed to set %s fan mode: %s", self._wrapped, err)

    async def _async_update_data(self) -> CoordinatorData:
        before = self._persisted_state()
        temps = self._read_sensors()
        house_avg = house_average(temps)
        fresh = len(temps)
        room_spread = spread(temps)
        humidity = self._read_humidity()
        wrapped = self._read_wrapped()
        # Once-only logging when a data source drops/returns (no per-poll spam). Observe-and-log only;
        # the regulation below is unchanged — a missing thermostat already yields no band, and zero
        # fresh sensors already trips the decide() failsafe.
        _log_availability(
            self._thermostat_avail,
            available=wrapped.present,
            on_lost=f"Wrapped thermostat {self._wrapped} is unavailable; multiroom control is paused until it returns",
            on_back=f"Wrapped thermostat {self._wrapped} is available again; resuming control",
        )
        _log_availability(
            self._sensors_avail,
            available=fresh > 0,
            on_lost=f"All target temperature sensors ({', '.join(self._sensors)}) are unavailable; holding the thermostat band until one returns",
            on_back="A target temperature sensor is available again; resuming regulation",
        )
        # Day/night schedule re-asserts the target at transitions (before decide() runs this tick).
        scheduled = self._apply_schedule()
        proposed, status, now_ts = self._evaluate(house_avg, wrapped, humidity)
        # Fan-circulate runs every tick regardless of HVAC mode (stratification builds when idle), so
        # it is gated independently of band availability — it fires even when there's no AUTO band.
        fan_proposed = decide_fan(room_spread, wrapped.is_circulating, self._config)

        # Actuate only when enabled; decide() still ran (above) so the shadow_* attributes keep
        # showing what it would do. last_change_ts advances only on a *successful* write.
        if self.enabled and proposed is not None and proposed.set_band and now_ts is not None:
            if await self._write_band(proposed):
                self._last_change_ts = now_ts

        # Fan write (no rate limit). The block reason is computed whenever a circulate is *wanted* —
        # independent of the switch — so the shadow surfaces "why circulation can't fire" even while
        # disabled (the shadow-everything norm; matches when shadow_proposed_fan shows intent). Only
        # the actual write is gated on enabled.
        fan_blocked = self._fan_block_reason(wrapped, fan_proposed) if fan_proposed.set_fan else None
        if self.enabled and fan_proposed.set_fan and fan_blocked is None:
            await self._write_fan(fan_proposed)

        if self._persisted_state() != before:
            self._save_state()

        return CoordinatorData(
            house_average=house_avg,
            hvac_mode=wrapped.hvac_mode,
            hvac_modes=wrapped.hvac_modes,
            band_low=wrapped.band_low,
            band_high=wrapped.band_high,
            temp_min=wrapped.temp_min,
            temp_max=wrapped.temp_max,
            target_temp_step=wrapped.target_temp_step,
            target=self._target,
            scheduled=scheduled,
            learned_offset=self._learned_offset,
            humidity=humidity,
            spread=room_spread,
            fan_proposed=fan_proposed,
            fan_blocked=fan_blocked,
            proposed=proposed,
            status=status,
            fresh_sensors=fresh,
            total_sensors=len(self._sensors),
            thermostat_present=wrapped.present,
            enabled=self.enabled,
        )


type MultiroomConfigEntry = ConfigEntry[MultiroomClimateCoordinator]
