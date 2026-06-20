"""Coordinator: poll the target sensors and expose the weighted house average."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_TARGET_SENSORS, DOMAIN

_LOGGER = logging.getLogger(__name__)
_UPDATE_INTERVAL = timedelta(seconds=60)


def house_average(temps: list[float]) -> float | None:
    """Equal-weight mean of the valid sensor temperatures, or None if there are none.

    Per-sensor weights are an options-flow feature for a later PR; for now every sensor counts equally.
    """
    return sum(temps) / len(temps) if temps else None


class MultiroomClimateCoordinator(DataUpdateCoordinator[dict]):
    """Polls the configured target sensors and exposes ``house_average`` + ``available``."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.title}",
            update_interval=_UPDATE_INTERVAL,
        )
        self._sensors: list[str] = entry.data[CONF_TARGET_SENSORS]

    async def _async_update_data(self) -> dict:
        temps: list[float] = []
        for sensor in self._sensors:
            state = self.hass.states.get(sensor)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            try:
                temps.append(float(state.state))
            except ValueError:
                continue
        avg = house_average(temps)
        return {"house_average": avg, "available": avg is not None}
