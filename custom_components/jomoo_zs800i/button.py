"""Explicit user-triggered status refresh only."""
from homeassistant.components.button import ButtonEntity

from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([JomooRefreshButton(entry.runtime_data, "refresh_status")])


class JomooRefreshButton(JomooEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_query()
