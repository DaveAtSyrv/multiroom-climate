"""Config flow for Multiroom Climate.

Minimal "get it installed" flow: pick the thermostat to wrap, the target temperature sensors, and
(optionally) a humidity sensor to enable cooling-season overcool. Tunables (targets, schedule, gains,
humidity tuning, fan) land in an options flow in a later PR — only the humidity *sensor* is picked
here; its setpoint/gain/cap stay on ``ControllerConfig`` defaults for now. The integration is
designed to work out of the box from just the thermostat + at least one temperature sensor.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_TARGET_SENSORS,
    DOMAIN,
)

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
