"""Protocol-derived switches displaying read-back state only."""
from homeassistant.components.switch import SwitchEntity

from .control import SHORT_SWITCHES
from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        JomooControlSwitch(entry.runtime_data, field) for field in SHORT_SWITCHES
    ])


class JomooControlSwitch(JomooEntity, SwitchEntity):
    _attr_icon = "mdi:toggle-switch"

    def __init__(self, coordinator, field):
        super().__init__(coordinator, f"control_{field}")
        self.field = field
        if field == "atmosphere_on":
            self._attr_icon = "mdi:lightbulb"

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get("fields", {}).get(self.field)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        attrs["control_field"] = self.field
        attrs["control_evidence"] = (
            "physical-verified" if self.field == "atmosphere_on" else "protocol-derived"
        )
        return attrs

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_control(self.field, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_control(self.field, False)
