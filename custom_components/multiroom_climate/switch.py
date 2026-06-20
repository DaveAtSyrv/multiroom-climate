"""Master enable switch — the kill switch that returns control to the thermostat.

Off (the default) means the coordinator computes but never writes: a fresh install is inert until
the user opts in, and toggling off hands the thermostat back to manual control immediately. The
switch owns nothing but its own restored state; the coordinator is the single decision/write point.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import MultiroomClimateCoordinator, MultiroomConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MultiroomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the master enable switch from a config entry."""
    async_add_entities([MultiroomEnableSwitch(entry.runtime_data, entry)])


class MultiroomEnableSwitch(RestoreEntity, SwitchEntity):
    """Turns the coordinator's actuation on/off and restores its last state across restarts."""

    _attr_entity_category = None
    _attr_icon = "mdi:thermostat-auto"

    def __init__(
        self, coordinator: MultiroomClimateCoordinator, entry: MultiroomConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_enable"
        self._attr_name = f"{entry.title} control"

    @property
    def is_on(self) -> bool:
        return self._coordinator.enabled

    async def async_added_to_hass(self) -> None:
        """Restore the last on/off state and push it into the coordinator."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == STATE_ON:
            # Resume the persisted target rather than re-seeding to "now" on a restart.
            self._coordinator.set_enabled(True, reseed=False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._coordinator.set_enabled(True)
        self.async_write_ha_state()
        await self._coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._coordinator.set_enabled(False)
        self.async_write_ha_state()
        await self._coordinator.async_request_refresh()
