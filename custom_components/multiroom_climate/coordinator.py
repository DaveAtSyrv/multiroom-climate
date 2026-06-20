"""Coordinator: poll the target sensors + wrapped thermostat, run the controller in shadow.

Per SPEC §5 the coordinator owns *all* the reads each tick — the room sensors (for the weighted
house average) and the wrapped thermostat (its HVAC mode, AUTO band, and temperature bounds). It
also runs the pure ``controller.decide()`` each tick and records the proposed ``Action`` **without
writing anything to the thermostat** (SPEC §6 step 5c, shadow mode). Actuation (turning the proposal
into a real ``climate.set_temperature``) plus durable persistence land in later PRs; here the
control state (learned offset, last target/change) lives in memory and the only output is
observability.

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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import convert
from homeassistant.util import dt as dt_util

from .const import CONF_CLIMATE_ENTITY, CONF_TARGET_SENSORS, DOMAIN
from .controller import Action, ControllerConfig, ControllerInputs, decide

_LOGGER = logging.getLogger(__name__)
_UPDATE_INTERVAL = timedelta(seconds=60)


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
    controller will slide once actuation lands. They are ``None`` in single-setpoint modes
    (heat/cool) or when the thermostat is gone, so actuation must skip ``decide()`` when either is
    ``None`` rather than assume a band exists. ``target``/``learned_offset``/``proposed`` are the
    shadow-mode outputs: what the controller *would* do, with nothing written.
    """

    house_average: float | None
    hvac_mode: HVACMode | None
    hvac_modes: tuple[HVACMode, ...]
    band_low: float | None
    band_high: float | None
    target: float | None
    learned_offset: float
    proposed: Action | None

    @property
    def available(self) -> bool:
        """Usable only when there's a house average to regulate toward."""
        return self.house_average is not None


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

        # Shadow control state (in-memory; durable persistence lands with actuation).
        self._target: float | None = None
        self._last_target: float | None = None
        self._learned_offset: float = 0.0
        self._last_change_ts: float = 0.0

    def _read_house_average(self) -> float | None:
        temps: list[float] = []
        for sensor in self._sensors:
            state = self.hass.states.get(sensor)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            value = convert(state.state, float)
            if value is not None:
                temps.append(value)
        return house_average(temps)

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

    def _shadow_decide(self, house_avg: float, wrapped: _WrappedReading) -> Action:
        """Run the real ``decide()`` and advance the control state as actuation will — except for
        the write. Persisting ``new_offset`` and stamping ``last_change_ts`` on a proposed band keep
        the rate limit and offset EMA behaving as they will once we actuate. The safety bounds come
        from the wrapped thermostat's own min/max for this tick.

        Two things are *not* exercised in shadow, by construction:
        - **Open-loop:** because nothing is written, the band never moves and the house never
          responds, so a sustained drift re-proposes the *same* trim each period rather than
          converging. ``shadow_proposed_band_*`` is "current band ± one step", not a trajectory.
        - **Feedforward:** the seeded target is constant here, so ``target != last_target`` never
          holds. ``_last_target`` is wired for 5d, when the target becomes user-settable and a change
          fires the feedforward jump.
        What *does* run live is the within-deadband offset learning — the genuine "67-to-hold-70"
        bias — which is the point of shadow mode and a good seed for 5d.
        """
        # Seed the target to the current operating point so the within-deadband learning branch runs
        # from the first tick (the actual user-settable target arrives with actuation).
        if self._target is None:
            self._target = house_avg
            self._last_target = house_avg

        config = replace(self._config, temp_min=wrapped.temp_min, temp_max=wrapped.temp_max)
        now_ts = dt_util.utcnow().timestamp()
        action = decide(
            ControllerInputs(
                available=True,
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
        return action

    async def _async_update_data(self) -> CoordinatorData:
        house_avg = self._read_house_average()
        wrapped = self._read_wrapped()

        # decide() needs a full AUTO band AND the equipment's temp bounds to clamp safely; without
        # either we can't (and at 5d won't) act, so skip it rather than guess.
        proposed: Action | None = None
        if (
            house_avg is not None
            and wrapped.band_low is not None
            and wrapped.band_high is not None
            and wrapped.temp_min is not None
            and wrapped.temp_max is not None
        ):
            proposed = self._shadow_decide(house_avg, wrapped)

        return CoordinatorData(
            house_average=house_avg,
            hvac_mode=wrapped.hvac_mode,
            hvac_modes=wrapped.hvac_modes,
            band_low=wrapped.band_low,
            band_high=wrapped.band_high,
            target=self._target,
            learned_offset=self._learned_offset,
            proposed=proposed,
        )


type MultiroomConfigEntry = ConfigEntry[MultiroomClimateCoordinator]
