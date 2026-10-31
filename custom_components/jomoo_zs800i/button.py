"""Explicit refresh and protocol-derived action buttons."""
from homeassistant.components.button import ButtonEntity

from .control import ACTIONS
from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([
        JomooRefreshButton(coordinator, "refresh_status"),
        *(JomooActionButton(coordinator, action) for action in ACTIONS),
    ])


class JomooRefreshButton(JomooEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_query()


class JomooActionButton(JomooEntity, ButtonEntity):
    _attr_icon = "mdi:toilet"

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        attrs["control_evidence"] = "protocol-derived"
        return attrs

    async def async_press(self) -> None:
        await self.coordinator.async_control(self.key)
