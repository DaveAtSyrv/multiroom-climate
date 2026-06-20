"""The Multiroom Climate integration.

A Home Assistant smart thermostat that regulates the home to a weighted average of chosen room
sensors and auto-learns the bias of the wrapped thermostat's own sensor. See SPEC.md.

At this stage the integration is installable (config flow + config entry) but forwards no platforms
yet — the coordinator and climate entity that run controller.decide() land in later PRs (SPEC §6).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Multiroom Climate from a config entry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
