"""Shared pytest fixtures.

``enable_custom_integrations`` (from pytest-homeassistant-custom-component) lets Home Assistant load
this repo's ``custom_components/`` during tests. The pure ``test_controller.py`` does not use ``hass``
and is unaffected.
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integration loading for all tests."""
    yield
