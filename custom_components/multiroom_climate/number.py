"""Learned-offset overrides — a manual escape hatch for the self-tuning bias.

The controller learns a band-to-house offset and applies it automatically. It is normally hands-off,
but a bad learned state (e.g. transient corruption after a large scheduled setback) would otherwise
require deleting and re-adding the integration to clear. These config-category numbers expose the
offsets so the user can reset one to 0 (relearn from scratch) or seed a known-good value in place.

The offset is **regime-dependent**, so there are two — one for cooling, one for heating (the wrapped
thermostat's own sensor reads a different bias relative to the band in each mode). Tucked under the
device's config entities — not something to touch in normal use. Each stays self-tuning, so a value
set here is a starting point the slow EMA then refines, not a permanent lock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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
    """Set up the cooling + heating learned-offset override numbers from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            MultiroomLearnedOffsetNumber(
                coordinator,
                entry,
                key="cool_offset",
                read=lambda c: c.cool_offset,
                write=coordinator.async_set_cool_offset,
            ),
            MultiroomLearnedOffsetNumber(
                coordinator,
                entry,
                key="heat_offset",
                read=lambda c: c.heat_offset,
                write=coordinator.async_set_heat_offset,
            ),
        ]
    )


class MultiroomLearnedOffsetNumber(
    CoordinatorEntity[MultiroomClimateCoordinator], NumberEntity
):
    """Read/override one of the controller's learned band-to-house offsets (config category).

    A ``CoordinatorEntity`` so the displayed value tracks the offset live as the controller keeps
    learning. The ``key`` selects which offset (``cool_offset``/``heat_offset``); the ``read``/``write``
    callables bind it to the matching coordinator accessor.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False  # advanced escape hatch — hidden unless sought out
    _attr_mode = NumberMode.BOX
    # The offset is a temperature *difference* in the system unit; a few degrees either way covers
    # every real bias. Wide enough to set any sane value, bounded so a fat-finger can't wreck control.
    _attr_native_min_value = -10.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.1

    def __init__(
        self,
        coordinator: MultiroomClimateCoordinator,
        entry: MultiroomConfigEntry,
        *,
        key: str,
        read: Callable[[MultiroomClimateCoordinator], float],
        write: Callable[[float], Awaitable[None]],
    ) -> None:
        super().__init__(coordinator)
        self._read = read
        self._write = write
        self._attr_translation_key = key
        # unique_id is per-offset ({entry}_cool_offset / _heat_offset). Upgrading from the single
        # {entry}_learned_offset entity is NOT auto-migrated: it's a config-category, disabled-by-default
        # diagnostic, so a registry row only exists if the user explicitly enabled it — in which case the
        # old one goes unavailable and can be deleted in the UI. Not worth an entity-registry migration
        # for a hidden override; the persisted *control state* migrates separately (see coordinator).
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> float:
        return self._read(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        await self._write(value)
        # Reflect the new value immediately; the coordinator's own refresh is debounced.
        self.async_write_ha_state()
