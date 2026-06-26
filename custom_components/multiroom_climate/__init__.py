"""The Multiroom Climate integration.

A Home Assistant smart thermostat that regulates the home to an average of chosen room
sensors and auto-learns the bias of the wrapped thermostat's own sensor. See SPEC.md.

It forwards a ``climate`` entity (the house thermostat) and a ``switch`` entity (the master enable).
With the switch on, the coordinator slides the wrapped thermostat's AUTO band to hold the house
average at the target; with it off, the integration only observes. The control state persists across
restarts, and removing the entry deletes that stored state.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import (
    MultiroomClimateCoordinator,
    MultiroomConfigEntry,
    build_store,
    thermostat_missing_issue_id,
)

_PLATFORMS = [Platform.CLIMATE, Platform.NUMBER, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Set up Multiroom Climate from a config entry."""
    coordinator = MultiroomClimateCoordinator(hass, entry)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Reload on any entry update (schedule options or reconfigured sensors) — config is read in the
    # coordinator ctor, so a single reload rebuilds it. Reconfigure relies on this instead of
    # async_update_reload_and_abort to avoid a double reload.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: MultiroomConfigEntry) -> None:
    """Reload the entry on any update (options *or* reconfigured data) so the coordinator rebuilds."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> bool:
    """Unload a config entry, clearing any standing 'thermostat missing' repair issue.

    Clearing on unload means reconfiguring to a different thermostat (reconfigure → reload → unload)
    resolves a stale issue cleanly; if the thermostat is still missing the next setup re-raises it.
    """
    ir.async_delete_issue(hass, DOMAIN, thermostat_missing_issue_id(entry.entry_id))
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MultiroomConfigEntry) -> None:
    """Delete the persisted control state when the entry is removed (don't orphan the file)."""
    await build_store(hass, entry).async_remove()
