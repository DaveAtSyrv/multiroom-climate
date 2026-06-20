"""Diagnostics for Multiroom Climate.

A downloadable snapshot for debugging and live tuning: the entry config, the durable control state
(learned bias, target, schedule change-detector), and the most recent computed tick. Nothing here is
sensitive — it's entity IDs and control numbers — so nothing is redacted, and the entity IDs are
exactly what makes the dump useful.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import MultiroomConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MultiroomConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "control_state": coordinator._persisted_state(),
        "last_tick": asdict(data) if data is not None else None,
    }
