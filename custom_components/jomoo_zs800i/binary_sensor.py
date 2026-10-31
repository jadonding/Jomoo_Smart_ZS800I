"""Source-derived binary snapshots; no control entities."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import JomooEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([JomooBinarySensor(entry.runtime_data, key)
                        for key in ("occupancy", "lid_open", "ring_open", "atmosphere_on")])


class JomooBinarySensor(JomooEntity, BinarySensorEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        if key == "occupancy":
            self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY
        elif key in ("lid_open", "ring_open"):
            self._attr_device_class = BinarySensorDeviceClass.OPENING
        else:
            self._attr_icon = "mdi:lightbulb"

    @property
    def is_on(self):
        return self.coordinator.data["fields"].get(self.key)

    @property
    def extra_state_attributes(self):
        return {**super().extra_state_attributes,
                "semantics": ("single_field_operation_verified_by_project_owner"
                              if self.key == "atmosphere_on"
                              else "source_derived_not_operation_verified"),
                "observed_at": self.coordinator.data["field_times"].get(self.key)}
