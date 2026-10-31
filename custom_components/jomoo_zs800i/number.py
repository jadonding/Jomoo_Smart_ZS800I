"""Integer heating levels; values are read back, never optimistic."""
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .control import LEVELS
from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        JomooLevelNumber(entry.runtime_data, field, *bounds)
        for field, bounds in LEVELS.items()
    ])


class JomooLevelNumber(JomooEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:format-list-numbered"

    def __init__(self, coordinator, field, minimum, maximum):
        super().__init__(coordinator, f"set_{field}")
        self.field = field
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("fields", {}).get(self.field)

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        attrs.update({"control_field": self.field, "control_evidence": "protocol-derived",
                      "observed_at": self.coordinator.data.get("field_times", {}).get(self.field),
                       "unit_note": "integer level, not degrees Celsius"})
        return attrs

    async def async_set_native_value(self, value: float) -> None:
        if not float(value).is_integer():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="control_invalid_value"
            )
        await self.coordinator.async_control(self.field, int(value))
