"""Learned-offset override — a manual escape hatch for the self-tuning bias.

The controller learns a band-to-house offset K and applies it automatically. It is normally hands-off,
but a bad learned state (e.g. transient corruption after a large scheduled setback) would otherwise
require deleting and re-adding the integration to clear. This config-category number exposes K so the
user can reset it to 0 (relearn from scratch) or seed a known-good value in place. Tucked under the
device's config entities — not something to touch in normal use. K stays self-tuning, so a value set
here is a starting point the slow EMA then refines, not a permanent lock.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import (
    MultiroomClimateCoordinator,
    MultiroomConfigEntry,
    build_device_info,
)

# All device I/O is funnelled through the single coordinator; entities never poll in parallel.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MultiroomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the learned-offset override number from a config entry."""
    async_add_entities([MultiroomLearnedOffsetNumber(entry.runtime_data, entry)])


class MultiroomLearnedOffsetNumber(
    CoordinatorEntity[MultiroomClimateCoordinator], NumberEntity
):
    """Read/override the controller's learned band-to-house offset K (config category).

    A ``CoordinatorEntity`` so the displayed value tracks K live as the controller keeps learning.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "learned_offset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False  # advanced escape hatch — hidden unless sought out
    _attr_mode = NumberMode.BOX
    # The offset is a temperature *difference* in the system unit; a few degrees either way covers
    # every real bias. Wide enough to set any sane value, bounded so a fat-finger can't wreck control.
    _attr_native_min_value = -10.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.1

    def __init__(
        self, coordinator: MultiroomClimateCoordinator, entry: MultiroomConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_learned_offset"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> float:
        return self.coordinator.learned_offset

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_learned_offset(value)
        # Reflect the new value immediately; the coordinator's own refresh is debounced.
        self.async_write_ha_state()
