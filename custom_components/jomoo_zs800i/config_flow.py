"""Manual address or exact-name Home Assistant Bluetooth discovery."""
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, LOCAL_NAME


class JomooConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _set_address(self, address):
        self._address = address.upper()
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            # MAC on Linux/HAOS, CoreBluetooth UUID on macOS. No scan/connection.
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self._set_address(address)
                return self.async_create_entry(title=f"JOMOO ZS800I ({self._address})",
                                               data={CONF_ADDRESS: self._address})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_ADDRESS): str}), errors=errors)

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        if discovery_info.name != LOCAL_NAME:
            return self.async_abort(reason="unsupported_device")
        await self._set_address(discovery_info.address)
        self.context["title_placeholders"] = {"name": f"JOMOO ZS800I ({self._address})"}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=f"JOMOO ZS800I ({self._address})",
                                           data={CONF_ADDRESS: self._address})
        return self.async_show_form(step_id="bluetooth_confirm",
                                    description_placeholders={"address": self._address})
