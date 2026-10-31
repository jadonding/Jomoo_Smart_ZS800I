"""Local JOMOO ZS800I controls and selectable BLE connection policy."""
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({vol.Required(CONF_ADDRESS): cv.string})
}, extra=vol.ALLOW_EXTRA)

from .coordinator import JomooCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON,
             Platform.SWITCH, Platform.NUMBER]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Optionally import an explicit address from YAML without connecting."""
    if DOMAIN in config:
        hass.async_create_task(hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=config[DOMAIN]
        ))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.title.startswith("JOMOO ZS800I ("):
        hass.config_entries.async_update_entry(
            entry, title=f"九牧 ZS800I ({entry.data[CONF_ADDRESS]})"
        )
    coordinator = JomooCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    async def stop_connection(_event):
        await coordinator.async_shutdown()
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_connection))
    coordinator.async_start_warmup()
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await entry.runtime_data.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
