"""Constants for the read-only 237371 integration."""
DOMAIN = "jomoo_zs800i"
MODEL = "237371"
LOCAL_NAME = "JOMOO_SMART_237371"
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
QUERY = bytes.fromhex("f3f4089200e2000000007cfc")
STATUS_COMMANDS = frozenset((0xFA, 0xE9, 0xEA))
NOTIFY_TIMEOUT = 8
CONNECT_TIMEOUT = 20
QUERY_RESULTS = (
    "never_queried", "querying", "valid_status_received", "not_discovered",
    "no_valid_status", "connection_timeout", "connection_error",
    "missing_characteristic", "notify_not_supported", "write_not_supported",
    "unexpected_error", "cancelled",
)
