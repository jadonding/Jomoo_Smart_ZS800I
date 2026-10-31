"""Shared cached-snapshot entity metadata."""
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODEL


class JomooEntity(CoordinatorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="九牧 JOMOO", model=f"ZS800I ({MODEL})", name="九牧智能马桶 ZS800I",
        )

    @property
    def available(self) -> bool:
        # A stored snapshot remains readable offline; it is not a live state.
        return True

    @property
    def extra_state_attributes(self):
        return {"snapshot_only": True,
                "last_status_time": self.coordinator.data.get("last_status_time"),
                "last_query_time": self.coordinator.data.get("last_query_time"),
                "last_query_result": self.coordinator.data.get("last_result"),
                "last_control_result": self.coordinator.data.get("last_control_result"),
                "last_control_name": self.coordinator.data.get("last_control_name"),
                "last_control_time": self.coordinator.data.get("last_control_time"),
                "bluetooth_connected": self.coordinator.data.get("bluetooth_connected", False),
                "last_connection_reused": self.coordinator.data.get("last_connection_reused"),
                "last_control_write_ms": self.coordinator.data.get("last_control_write_ms"),
                "last_control_write_complete_ms": self.coordinator.data.get("last_control_write_complete_ms"),
                "last_control_total_ms": self.coordinator.data.get("last_control_total_ms")}
