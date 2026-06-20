"""Coordinator: poll the target sensors + wrapped thermostat, expose the regulated view.

Per SPEC §5 the coordinator owns *all* the reads each tick — the room sensors (for the weighted
house average) and the wrapped thermostat (its HVAC mode now; its setpoints when actuation lands).
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
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import convert

from .const import CONF_CLIMATE_ENTITY, CONF_TARGET_SENSORS, DOMAIN

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
class CoordinatorData:
    """The regulated view computed each tick: the house average + the wrapped thermostat's state.

    ``band_low``/``band_high`` are the wrapped thermostat's current AUTO setpoints — the band the
    controller will slide once actuation lands. Surfaced now (read-only) so the band can be watched
    alongside the house average before any writes happen. They are ``None`` in single-setpoint modes
    (heat/cool) or when the thermostat is gone, so actuation must skip ``decide()`` when either is
    ``None`` rather than assume a band exists.
    """

    house_average: float | None
    hvac_mode: HVACMode | None
    hvac_modes: tuple[HVACMode, ...]
    band_low: float | None
    band_high: float | None

    @property
    def available(self) -> bool:
        """Usable only when there's a house average to regulate toward."""
        return self.house_average is not None


class MultiroomClimateCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Polls the target sensors + wrapped thermostat and exposes a ``CoordinatorData`` each tick."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=_UPDATE_INTERVAL,
        )
        self._sensors: list[str] = entry.data[CONF_TARGET_SENSORS]
        self._wrapped: str = entry.data[CONF_CLIMATE_ENTITY]

    async def _async_update_data(self) -> CoordinatorData:
        temps: list[float] = []
        for sensor in self._sensors:
            state = self.hass.states.get(sensor)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            try:
                temps.append(float(state.state))
            except ValueError:
                continue

        hvac_mode: HVACMode | None = None
        hvac_modes: tuple[HVACMode, ...] = ()
        band_low: float | None = None
        band_high: float | None = None
        wrapped = self.hass.states.get(self._wrapped)
        if wrapped is not None:
            if wrapped.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                hvac_mode = _to_hvac_mode(wrapped.state)
            hvac_modes = tuple(
                mode
                for mode in (
                    _to_hvac_mode(value)
                    for value in wrapped.attributes.get("hvac_modes", [])
                )
                if mode is not None
            )
            band_low = convert(wrapped.attributes.get(ATTR_TARGET_TEMP_LOW), float)
            band_high = convert(wrapped.attributes.get(ATTR_TARGET_TEMP_HIGH), float)

        return CoordinatorData(
            house_average=house_average(temps),
            hvac_mode=hvac_mode,
            hvac_modes=hvac_modes,
            band_low=band_low,
            band_high=band_high,
        )


type MultiroomConfigEntry = ConfigEntry[MultiroomClimateCoordinator]
