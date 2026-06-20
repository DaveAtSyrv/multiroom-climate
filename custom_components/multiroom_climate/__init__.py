"""The Multiroom Climate integration.

A Home Assistant smart thermostat that regulates the home to a weighted average of chosen room
sensors and auto-learns the bias of the wrapped thermostat's own sensor. See SPEC.md.

At this stage the climate entity is read-only — it observes the weighted house average and mirrors
the wrapped thermostat. Driving the thermostat with controller.decide() lands in the next PR (SPEC §6).
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MultiroomClimateCoordinator, MultiroomConfigEntry

_PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Set up Multiroom Climate from a config entry."""
    coordinator = MultiroomClimateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
