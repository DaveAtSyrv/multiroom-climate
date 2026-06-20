"""Climate entity: the user-facing thermostat for the weighted house average.

``current_temperature`` is the weighted house average and ``target_temperature`` is the single
temperature to hold it at; setting it hands the new target to the coordinator, which slides the
wrapped thermostat's AUTO band to reach it (when the master switch is on). HVAC mode mirrors the
wrapped thermostat. The single setpoint is deliberate — the wrapped band is the coordinator's
actuation interface, so the user only ever picks *one* house temperature.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
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
    """Set up the Multiroom Climate entity from a config entry."""
    async_add_entities([MultiroomClimateEntity(entry.runtime_data, entry)])


class MultiroomClimateEntity(
    CoordinatorEntity[MultiroomClimateCoordinator], ClimateEntity
):
    """Renders the house average + a single settable target; mirrors the wrapped HVAC mode."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

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
        """Available while the wrapped thermostat is reachable — not gated on sensor freshness, so
        the failsafe/status stays visible when sensors go stale.

        ``current_temperature`` simply reports ``None`` (unknown) when no sensor is fresh.
        """
        return super().available and self.coordinator.data.thermostat_present

    @property
    def current_temperature(self) -> float | None:
        """The weighted house average the controller regulates toward."""
        return self.coordinator.data.house_average

    @property
    def target_temperature(self) -> float | None:
        """The single temperature the user wants the house average held at."""
        return self.coordinator.data.target

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Hand a new target to the coordinator (it feedforward-jumps the band to reach it)."""
        if (target := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_target(target)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Mirror the wrapped thermostat's current mode."""
        return self.coordinator.data.hvac_mode

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Mirror the modes the wrapped thermostat advertises."""
        return list(self.coordinator.data.hvac_modes)

    @property
    def extra_state_attributes(self) -> dict[str, float | int | str | None]:
        """Surface the wrapped band and the controller's decision for observability.

        ``band_low``/``band_high`` are the thermostat's current AUTO band. ``shadow_status`` is what
        the controller is doing (or why it isn't), ``shadow_sensors_*`` expose sensor degradation,
        and the remaining ``shadow_*`` keys are its decision — the target it holds, learned bias
        offset, the band it would set, and any failsafe message. The ``shadow_`` prefix marks these
        as diagnostics; the proposed band is actually written to the thermostat when the switch is on.
        """
        data = self.coordinator.data
        attrs: dict[str, float | int | str | None] = {
            "band_low": data.band_low,
            "band_high": data.band_high,
            "shadow_status": data.status,
            "shadow_sensors_fresh": data.fresh_sensors,
            "shadow_sensors_total": data.total_sensors,
            "shadow_target": data.target,
            "shadow_learned_offset": round(data.learned_offset, 2),
            "shadow_humidity": data.humidity,  # RH decide() saw (None = no sensor/stale → overcool off)
        }
        proposed = data.proposed
        if proposed is not None:
            if proposed.set_band:
                attrs["shadow_proposed_band_low"] = proposed.band_low
                attrs["shadow_proposed_band_high"] = proposed.band_high
            if proposed.notify:
                attrs["shadow_notify"] = proposed.notify
        return attrs
