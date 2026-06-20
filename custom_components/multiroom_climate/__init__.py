"""The Multiroom Climate integration.

A Home Assistant smart thermostat that regulates the home to a weighted average of chosen room
sensors and auto-learns the bias of the wrapped thermostat's own sensor. See SPEC.md.

It forwards a ``climate`` entity (the house thermostat) and a ``switch`` entity (the master enable).
With the switch on, the coordinator slides the wrapped thermostat's AUTO band to hold the house
average at the target; with it off, the integration only observes. The control state persists across
restarts, and removing the entry deletes that stored state.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MultiroomClimateCoordinator, MultiroomConfigEntry, build_store

_PLATFORMS = [Platform.CLIMATE, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Set up Multiroom Climate from a config entry."""
    coordinator = MultiroomClimateCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> None:
    """Delete the persisted control state when the entry is removed (don't orphan the file)."""
    await build_store(hass, entry).async_remove()
