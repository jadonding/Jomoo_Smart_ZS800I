"""Raw diagnostic snapshots and explicitly source-derived levels."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .const import MODEL
from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([JomooSensor(coordinator, key) for key in (
        "last_status_time", "raw_fa", "raw_e9", "raw_ea", "address", "model",
        "last_query_result", "water_level", "seat_level", "air_level")])


class JomooSensor(JomooEntity, SensorEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "last_status_time":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:toilet"

    @property
    def native_value(self):
        data = self.coordinator.data
        if self.key == "last_status_time":
            return self.coordinator.last_status_time
        if self.key.startswith("raw_"):
            return data["frames"].get(self.key[4:].upper())
        if self.key == "address":
            return self.coordinator.address
        if self.key == "model":
            return MODEL
        if self.key == "last_query_result":
            return data["last_result"]
        return data["fields"].get(self.key)

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        if self.key.startswith("raw_"):
            attrs["observed_at"] = self.coordinator.data["frame_times"].get(self.key[4:].upper())
            attrs["format"] = "uppercase HEX, no spaces, max 42 characters"
        elif self.key in ("water_level", "seat_level", "air_level"):
            attrs.update({"semantics": "source_derived_not_operation_verified",
                          "unit_note": "level, not degrees Celsius",
                          "observed_at": self.coordinator.data["field_times"].get(self.key)})
        return attrs
