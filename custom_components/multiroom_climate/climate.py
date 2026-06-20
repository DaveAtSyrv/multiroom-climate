"""Read-only climate entity: expose the weighted house average, mirror the wrapped thermostat.

This is the observe layer (SPEC §6 step 5). It writes NOTHING to the wrapped thermostat and
supports no setpoint features — ``decide()`` actuation lands in the next PR. Its only jobs are to
surface ``current_temperature`` (the coordinator's weighted house average) and to mirror the
wrapped thermostat's HVAC mode so the pair reads sensibly in the UI.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CLIMATE_ENTITY
from .coordinator import MultiroomClimateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the read-only Multiroom Climate entity from a config entry."""
    async_add_entities([MultiroomClimateEntity(entry.runtime_data, entry)])


class MultiroomClimateEntity(
    CoordinatorEntity[MultiroomClimateCoordinator], ClimateEntity
):
    """Observes the house average and mirrors the wrapped thermostat's HVAC mode (no writes)."""

    _attr_supported_features = ClimateEntityFeature(0)
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self, coordinator: MultiroomClimateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._wrapped = entry.data[CONF_CLIMATE_ENTITY]
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title

    @property
    def available(self) -> bool:
        """Available only when the coordinator has a usable house average."""
        return super().available and bool(self.coordinator.data.get("available"))

    @property
    def current_temperature(self) -> float | None:
        """The weighted house average the controller regulates toward."""
        return self.coordinator.data.get("house_average")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Mirror the wrapped thermostat's current mode."""
        state = self.hass.states.get(self._wrapped)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return HVACMode(state.state)
        except ValueError:
            return None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Mirror the modes the wrapped thermostat advertises."""
        state = self.hass.states.get(self._wrapped)
        if state is None:
            return []
        modes = []
        for mode in state.attributes.get("hvac_modes", []):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                continue
        return modes
