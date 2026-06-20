"""Coordinator: poll the target sensors + wrapped thermostat, run the controller in shadow.

Per SPEC §5 the coordinator owns *all* the reads each tick — the room sensors (for the weighted
house average) and the wrapped thermostat (its HVAC mode + AUTO band). It also runs the pure
``controller.decide()`` each tick and records the proposed ``Action`` **without writing anything to
the thermostat** (SPEC §6 step 5c, shadow mode). Actuation (turning the proposal into a real
``climate.set_temperature``) plus durable persistence land in the next PR; here the control state
(learned offset, last target/change) lives in memory and the only output is observability.

Entities render ``CoordinatorData``; they never reach around the coordinator to read state directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import convert
from homeassistant.util import dt as dt_util

from .const import CONF_CLIMATE_ENTITY, CONF_TARGET_SENSORS, DOMAIN
from .controller import Action, ControllerConfig, ControllerInputs, decide

_LOGGER = logging.getLogger(__name__)
_UPDATE_INTERVAL = timedelta(seconds=60)

# ControllerConfig's default temp bounds are in °C; widen them for a Fahrenheit system so shadow
# proposals aren't clamped to nonsense. (Reading the wrapped thermostat's own min/max is a later
# refinement.)
_FAHRENHEIT_BOUNDS = {"temp_min": 45.0, "temp_max": 95.0}


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

        bounds = (
            _FAHRENHEIT_BOUNDS
            if hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
            else {}
        )
        self._config = ControllerConfig(**bounds)

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

    def _read_wrapped(self) -> tuple[HVACMode | None, tuple[HVACMode, ...], float | None, float | None]:
        wrapped = self.hass.states.get(self._wrapped)
        if wrapped is None:
            return None, (), None, None
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
        band_low = convert(wrapped.attributes.get(ATTR_TARGET_TEMP_LOW), float)
        band_high = convert(wrapped.attributes.get(ATTR_TARGET_TEMP_HIGH), float)
        return hvac_mode, hvac_modes, band_low, band_high

    def _shadow_decide(
        self, house_avg: float, band_low: float, band_high: float
    ) -> Action:
        """Run the real ``decide()`` and advance the control state exactly as actuation would —
        except for the write. Mirroring the full state machine (persisting ``new_offset``, stamping
        ``last_change_ts`` on a proposed band) keeps the shadow faithful: the rate limit engages and
        the learned offset evolves just as they will once we actuate.
        """
        # Seed the target to the current operating point so the within-deadband learning branch runs
        # from the first tick (the actual user-settable target arrives with actuation).
        if self._target is None:
            self._target = house_avg
            self._last_target = house_avg

        now_ts = dt_util.utcnow().timestamp()
        action = decide(
            ControllerInputs(
                available=True,
                house_average=house_avg,
                target=self._target,
                band_low=band_low,
                band_high=band_high,
                now_ts=now_ts,
                last_change_ts=self._last_change_ts,
                learned_offset=self._learned_offset,
                last_target=self._last_target,
            ),
            self._config,
        )
        if action.new_offset is not None:
            self._learned_offset = action.new_offset
        if action.set_band:
            self._last_change_ts = now_ts
        self._last_target = self._target
        return action

    async def _async_update_data(self) -> CoordinatorData:
        house_avg = self._read_house_average()
        hvac_mode, hvac_modes, band_low, band_high = self._read_wrapped()

        proposed: Action | None = None
        if house_avg is not None and band_low is not None and band_high is not None:
            proposed = self._shadow_decide(house_avg, band_low, band_high)

        return CoordinatorData(
            house_average=house_avg,
            hvac_mode=hvac_mode,
            hvac_modes=hvac_modes,
            band_low=band_low,
            band_high=band_high,
            target=self._target,
            learned_offset=self._learned_offset,
            proposed=proposed,
        )


type MultiroomConfigEntry = ConfigEntry[MultiroomClimateCoordinator]
