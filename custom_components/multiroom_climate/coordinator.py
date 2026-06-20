"""Coordinator: poll the target sensors + wrapped thermostat, run the controller in shadow.

Per SPEC §5 the coordinator owns *all* the reads each tick — the room sensors (for the weighted
house average) and the wrapped thermostat (its HVAC mode, AUTO band, and temperature bounds). It
also runs the pure ``controller.decide()`` each tick and records the proposed ``Action`` **without
writing anything to the thermostat** (SPEC §6 step 5c, shadow mode). Turning the proposal into a
real ``climate.set_temperature`` plus durable persistence land in later PRs.

Sensor availability + failsafe (SPEC §3/§4.8): a fresh weighted average needs at least one usable
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
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import convert
from homeassistant.util import dt as dt_util

from .const import CONF_CLIMATE_ENTITY, CONF_TARGET_SENSORS, DOMAIN
from .controller import Action, ControllerConfig, ControllerInputs, decide

_LOGGER = logging.getLogger(__name__)
_UPDATE_INTERVAL = timedelta(seconds=60)

# Status strings for the cases where decide() doesn't run (it owns its own reasons otherwise).
_STATUS_NO_BAND = "no_thermostat_band"
_STATUS_WAITING = "waiting_for_first_reading"

# Persistence: the learned offset converges via a slow EMA, so it's expensive to relearn after a
# restart. Debounced writes batch the per-tick offset nudges to roughly one disk write per delay.
_STORE_VERSION = 1
_SAVE_DELAY_S = 600.0


def build_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, float | None]]:
    """The per-config-entry Store holding the coordinator's control state.

    A module-level factory so entry removal can delete the file without constructing a coordinator.
    """
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")


def house_average(temps: list[float]) -> float | None:
    """Equal-weight mean of the valid sensor temperatures, or None if there are none.

    Per-sensor weights are an options-flow feature for a later PR; for now every sensor counts equally.
    """
    return sum(temps) / len(temps) if temps else None


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

    @property
    def present(self) -> bool:
        """Whether the wrapped thermostat exists in a known HVAC mode (vs missing/unavailable)."""
        return self.hvac_mode is not None

    @property
    def has_band_and_bounds(self) -> bool:
        """Whether decide() can run: a full AUTO band plus the equipment's temp bounds to clamp to."""
        return None not in (self.band_low, self.band_high, self.temp_min, self.temp_max)


# The "thermostat missing/unavailable" reading — shareable because the dataclass is frozen.
_NO_READING = _WrappedReading(
    hvac_mode=None,
    hvac_modes=(),
    band_low=None,
    band_high=None,
    temp_min=None,
    temp_max=None,
)


@dataclass(frozen=True)
class CoordinatorData:
    """The regulated view computed each tick: the house average + the wrapped thermostat's state.

    ``band_low``/``band_high`` are the wrapped thermostat's current AUTO setpoints — the band the
    controller will slide once actuation lands. ``target``/``learned_offset``/``proposed`` are the
    shadow-mode outputs: what the controller *would* do, with nothing written. ``status`` is the
    one-word reason for this tick (``decide()``'s reason, or why it didn't run); ``fresh_sensors``/
    ``total_sensors`` expose sensor degradation; ``thermostat_present`` drives entity availability.
    """

    house_average: float | None
    hvac_mode: HVACMode | None
    hvac_modes: tuple[HVACMode, ...]
    band_low: float | None
    band_high: float | None
    target: float | None
    learned_offset: float
    proposed: Action | None
    status: str
    fresh_sensors: int
    total_sensors: int
    thermostat_present: bool


class MultiroomClimateCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Polls sensors + wrapped thermostat, runs ``decide()`` in shadow, exposes a ``CoordinatorData``."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=_UPDATE_INTERVAL,
        )
        self._sensors: list[str] = entry.data[CONF_TARGET_SENSORS]
        self._wrapped: str = entry.data[CONF_CLIMATE_ENTITY]

        # Base tunables (deadband, gain, rate limit, EMA). The safety bounds (temp_min/temp_max) are
        # overridden each tick from the wrapped thermostat's own min_temp/max_temp — already in the
        # system unit and correct for the actual equipment — so the defaults here are just a base.
        self._config = ControllerConfig()

        # Control state — restored from disk in async_load_state(), then kept in memory and saved
        # back (debounced) as it evolves. Persisting it means the learned bias and target survive
        # restarts instead of relearning from scratch.
        self._store = build_store(hass, entry)
        self._target: float | None = None
        self._last_target: float | None = None
        self._learned_offset: float = 0.0
        self._last_change_ts: float = 0.0

    async def async_load_state(self) -> None:
        """Restore persisted control state before the first refresh.

        Restoring ``target`` means we don't re-seed it to the current house average on restart.
        Restoring ``last_target`` keeps the feedforward gate sound once 5d makes the target
        user-settable — today the two are always equal, so feedforward is inert either way.
        """
        stored = await self._store.async_load()
        if not stored:
            return
        self._learned_offset = stored.get("learned_offset", 0.0)
        self._target = stored.get("target")
        self._last_target = stored.get("last_target")
        self._last_change_ts = stored.get("last_change_ts", 0.0)

    @callback
    def _persisted_state(self) -> dict[str, float | None]:
        return {
            "learned_offset": self._learned_offset,
            "target": self._target,
            "last_target": self._last_target,
            "last_change_ts": self._last_change_ts,
        }

    def _save_state(self) -> None:
        """Debounced write of the control state to disk (the ``.storage`` file, not the thermostat)."""
        self._store.async_delay_save(self._persisted_state, _SAVE_DELAY_S)

    def _read_sensors(self) -> tuple[float | None, int]:
        """Return the weighted house average (None if no sensor is fresh) and the fresh count."""
        temps: list[float] = []
        for sensor in self._sensors:
            state = self.hass.states.get(sensor)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            value = convert(state.state, float)
            if value is not None:
                temps.append(value)
        return house_average(temps), len(temps)

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
        )

    def _evaluate(
        self, house_avg: float | None, wrapped: _WrappedReading
    ) -> tuple[Action | None, str]:
        """Decide what (if anything) to propose this tick, with a status for observability.

        Three cases once the thermostat advertises a band + bounds (else we can't act at all):
        - a fresh house average → seed-if-needed and run the normal control tick;
        - no fresh average but we were already regulating → ``decide(available=False)`` failsafe;
        - no fresh average and never seeded → wait (skip *before* building ``ControllerInputs`` so a
          ``None`` target can't reach the dataclass).
        """
        if not wrapped.has_band_and_bounds:
            return None, _STATUS_NO_BAND
        if house_avg is not None:
            action = self._run_decide(house_avg, wrapped, available=True)
            return action, action.reason
        if self._target is not None:
            # Was regulating, then lost every sensor → failsafe (house_average is a don't-care here).
            action = self._run_decide(self._target, wrapped, available=False)
            return action, action.reason
        return None, _STATUS_WAITING

    def _run_decide(
        self, house_avg: float, wrapped: _WrappedReading, *, available: bool
    ) -> Action:
        """Run the real ``decide()`` and advance the control state as actuation will — except for
        the write. Persisting ``new_offset`` and stamping ``last_change_ts`` on a proposed band keep
        the rate limit and offset EMA behaving as they will once we actuate. Safety bounds come from
        the wrapped thermostat's own min/max for this tick.

        When ``available`` is False (lost all sensors mid-regulation) ``decide()`` short-circuits to
        the failsafe: freeze + a would-notify message, no learning — so ``house_avg`` is unused and
        the caller passes the retained target as a placeholder.

        Not exercised in shadow, by construction: the loop is **open-loop** (nothing is written, so a
        sustained drift re-proposes the *same* trim rather than converging), and **feedforward** never
        fires (the seeded target is constant; ``_last_target`` is wired for 5d's settable target).
        The within-deadband offset learning — the genuine "67-to-hold-70" bias — is what runs live.
        """
        # Snapshot the persisted fields, then save iff any changed this tick — robust to which branch
        # mutated what (and to fields added later) without a hand-maintained flag.
        before = self._persisted_state()

        # Seed the target to the current operating point so the within-deadband learning branch runs
        # from the first tick (the actual user-settable target arrives with actuation).
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
            ),
            config,
        )
        if action.new_offset is not None:
            self._learned_offset = action.new_offset
        if action.set_band:
            self._last_change_ts = now_ts
        self._last_target = self._target
        if self._persisted_state() != before:
            self._save_state()
        return action

    async def _async_update_data(self) -> CoordinatorData:
        house_avg, fresh = self._read_sensors()
        wrapped = self._read_wrapped()
        proposed, status = self._evaluate(house_avg, wrapped)

        return CoordinatorData(
            house_average=house_avg,
            hvac_mode=wrapped.hvac_mode,
            hvac_modes=wrapped.hvac_modes,
            band_low=wrapped.band_low,
            band_high=wrapped.band_high,
            target=self._target,
            learned_offset=self._learned_offset,
            proposed=proposed,
            status=status,
            fresh_sensors=fresh,
            total_sensors=len(self._sensors),
            thermostat_present=wrapped.present,
        )


type MultiroomConfigEntry = ConfigEntry[MultiroomClimateCoordinator]
