"""Config flow for Multiroom Climate.

Minimal "get it installed" flow: pick the thermostat to wrap, the target temperature sensors, and
(optionally) a humidity sensor to enable cooling-season overcool. The first options-flow surface lands
the day/night schedule here (temps + start times + optimal-start lead); remaining tunables (gains,
humidity tuning, fan) follow in later PRs. The integration is designed to work out of the box from
just the thermostat + at least one temperature sensor — the schedule is opt-in.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DAY_START,
    CONF_DAY_TEMP,
    CONF_HUMIDITY_SENSOR,
    CONF_NIGHT_START,
    CONF_NIGHT_TEMP,
    CONF_OPTIMAL_START_LEAD,
    CONF_SCHEDULE_ENABLED,
    CONF_TARGET_SENSORS,
    DOMAIN,
)
from .controller import ControllerConfig

# The only schedule default with no engine equivalent: ControllerConfig's setpoints are °C, so a
# Fahrenheit install needs its own pair (≈70/64) rather than defaulting to a nonsensical "21°F". Every
# other default (°C temps, start times, lead) is derived from ControllerConfig in _schedule_schema so
# the form prefill can't drift from what the engine actually runs.
_DEFAULT_DAY_NIGHT_F = (70.0, 64.0)


def _minutes_to_time(minutes: float) -> str:
    """Format minutes-since-midnight as the TimeSelector's ``"HH:MM:SS"`` (inverse of _time_to_minutes)."""
    hours, mins = divmod(int(minutes), 60)
    return f"{hours:02d}:{mins:02d}:00"

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="Multiroom Climate"): str,
        vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="climate"),
        ),
        vol.Required(CONF_TARGET_SENSORS): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class="temperature", multiple=True
            ),
        ),
        # Optional: one RH sensor. When set, cooling overcools while humid (see controller._overcool).
        vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="humidity"),
        ),
    }
)


class MultiroomClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow for Multiroom Climate."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MultiroomClimateOptionsFlow:
        """Expose the options flow (day/night schedule)."""
        return MultiroomClimateOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_TARGET_SENSORS]:
                # Required enforces presence, not non-emptiness; an empty average is meaningless.
                errors[CONF_TARGET_SENSORS] = "no_sensors"
            else:
                # One controller per wrapped thermostat — keying on it prevents duplicate setups.
                await self.async_set_unique_id(user_input[CONF_CLIMATE_ENTITY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)


def _schedule_schema(unit: str, options: dict[str, Any]) -> vol.Schema:
    """Build the schedule options schema, pre-filled from saved ``options`` (or unit-aware defaults).

    Temps are collected in the *system unit* so there's no runtime conversion: the user types °F on a
    Fahrenheit install and we store °F, which the unit-agnostic controller consumes as-is. The Fahrenheit
    default pair (≈70/64) keeps an F install from defaulting to a nonsensical "21".
    """
    base = ControllerConfig()
    fahrenheit = unit == UnitOfTemperature.FAHRENHEIT
    default_day, default_night = (
        _DEFAULT_DAY_NIGHT_F if fahrenheit else (base.day_temp, base.night_temp)
    )
    temp_min, temp_max = (40.0, 95.0) if fahrenheit else (5.0, 35.0)
    temp_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=temp_min, max=temp_max, step=0.5, unit_of_measurement=unit, mode="box"
        )
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_SCHEDULE_ENABLED,
                default=options.get(CONF_SCHEDULE_ENABLED, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_DAY_TEMP, default=options.get(CONF_DAY_TEMP, default_day)
            ): temp_selector,
            vol.Required(
                CONF_NIGHT_TEMP, default=options.get(CONF_NIGHT_TEMP, default_night)
            ): temp_selector,
            vol.Required(
                CONF_DAY_START,
                default=options.get(CONF_DAY_START, _minutes_to_time(base.day_start_min)),
            ): selector.TimeSelector(),
            vol.Required(
                CONF_NIGHT_START,
                default=options.get(CONF_NIGHT_START, _minutes_to_time(base.night_start_min)),
            ): selector.TimeSelector(),
            vol.Required(
                CONF_OPTIMAL_START_LEAD,
                default=options.get(CONF_OPTIMAL_START_LEAD, int(base.optimal_start_lead_min)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=240, step=5, unit_of_measurement="min", mode="box"
                )
            ),
        }
    )


class MultiroomClimateOptionsFlow(OptionsFlow):
    """Day/night schedule options. Saved values are read into ``ControllerConfig`` on the next reload."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show / store the schedule. Equal start times degrade to all-night — handled by the engine."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        unit = self.hass.config.units.temperature_unit
        return self.async_show_form(
            step_id="init",
            data_schema=_schedule_schema(unit, dict(self.config_entry.options)),
        )
