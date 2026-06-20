"""Read-only climate entity: expose the weighted house average, mirror the wrapped thermostat.

This is the observe layer (SPEC §6 step 5). It writes NOTHING to the wrapped thermostat and
supports no setpoint features — ``decide()`` actuation lands in the next PR. It only renders the
``CoordinatorData`` the coordinator computes: ``current_temperature`` (the weighted house average)
and the wrapped thermostat's HVAC mode, so the pair reads sensibly in the UI.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MultiroomClimateCoordinator, MultiroomConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MultiroomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the read-only Multiroom Climate entity from a config entry."""
    async_add_entities([MultiroomClimateEntity(entry.runtime_data, entry)])


class MultiroomClimateEntity(
    CoordinatorEntity[MultiroomClimateCoordinator], ClimateEntity
):
    """Renders the house average and mirrors the wrapped thermostat's HVAC mode (no writes)."""

    _attr_supported_features = ClimateEntityFeature(0)

    def __init__(
        self, coordinator: MultiroomClimateCoordinator, entry: MultiroomConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title

    @property
    def temperature_unit(self) -> str:
        """Report in the system unit — the coordinator already reads sensors in that unit."""
        return self.hass.config.units.temperature_unit

    @property
    def available(self) -> bool:
        """Available only when the coordinator has a usable house average."""
        return super().available and self.coordinator.data.available

    @property
    def current_temperature(self) -> float | None:
        """The weighted house average the controller regulates toward."""
        return self.coordinator.data.house_average

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Mirror the wrapped thermostat's current mode."""
        return self.coordinator.data.hvac_mode

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Mirror the modes the wrapped thermostat advertises."""
        return list(self.coordinator.data.hvac_modes)
