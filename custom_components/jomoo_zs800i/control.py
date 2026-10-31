"""Bounded, source-derived controls for model 237371 (ZS800I, bleProtocol 0).

Evidence: upstream/ToiletController.analysis.js:171-245, 334-343, 398-459,
481-544, 651-686, 949-1019, 1138-1144; upstream/device_enum.js:10617-10732.
All offsets below are zero-based FULL-frame offsets, never payload offsets.

19-byte frames reproduce the official ordinary (no timer/custom user) template.
They preserve mapped settings, NOT all unknown bits: FA is not a TX mirror.
FA[6] bits 0/2 are carried through from raw state, based on the shared E2/TX
layout, not separately verified FA semantics. Reserved TX bits are source-zero.
The caller MUST ensure FA/E9/EA freshness and serialize actual device writes.
No transport, arbitrary raw command, retry, clock, BLE or HA dependency exists.
"""
from __future__ import annotations

from .protocol import valid_frame

# lid_open is resolved to 17 or 19 using the latest EA ring state.
# ring_close=17 opens the lid and closes the ring, not an independent ring move.
ACTIONS = {
    "stop": 0, "flush_large": 14, "flush_small": 15,
    "lid_open": 17, "lid_close": 16, "ring_open": 19, "ring_close": 17,
    "wash_hip": 8, "wash_woman": 11, "wash_auto_hip": 12,
    "wash_auto_woman": 7, "dry": 4,
}

# Name -> (1F subcommand, ON, OFF). Every item has an FA readback.
# No open-loop bubble: model 237371 hasOpenCoverBubble=false.
SHORT_SWITCHES = {
    "atmosphere_on": (0x33, 0x20, 0x10),  # FA[11].4
    "foot_sensor": (0x24, 1, 2),          # FA[13].5
    "prewet": (0x30, 4, 0),              # FA[12].5; no seat-too-long on 237371
    "auto_bubble": (0x20, 2, 4),         # FA[12].3
    "uv_water": (0x21, 0x40, 0x20),      # FA[11].5
    "close_flush": (0x25, 1, 2),         # FA[10].1
    "auto_small_flush": (0x25, 4, 8),    # FA[13].4
}
LEVELS = {"water_level": (0, 5), "seat_level": (0, 5), "air_level": (0, 3)}


def _finish(command: int, payload: list[int]) -> bytes:
    frame = bytearray((0xF3, 0xF4, len(payload) + 4, 0x92, 0x21, command, *payload))
    frame.extend((sum(frame[2:]) & 0xFF, 0xFC))
    return bytes(frame)


def _level(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} requires an integer in {minimum}..{maximum}")
    return value


def _request(name: str, value: object) -> None:
    if name in SHORT_SWITCHES:
        if type(value) is not bool:
            raise ValueError(f"{name} requires bool")
    elif name in LEVELS:
        _level(value, *LEVELS[name], name)
    elif name in ACTIONS:
        if value is not None:
            raise ValueError(f"{name} takes no value")
    else:
        raise ValueError(f"Unsupported 237371 control: {name}")


def _ring(fields: dict) -> bool:
    ring = fields.get("ring_open")
    if type(ring) is not bool:
        raise ValueError("lid_open requires fresh EA ring_open bool")
    return ring


def build_control(name: str, value: object = None, fa: bytes | None = None,
                  fields: dict | None = None) -> bytes:
    """Build one allowlisted frame; invalid/incomplete baselines fail closed.

    Short switches need no baseline. Ordinary 19-byte actions/settings require
    a complete validated FA payload (at least 8 bytes), including for stop.
    is_woman in fields tracks latest E9 wash mode; explicit wash actions override
    it. lid_close closes BOTH lid/ring; ring_close opens lid and closes ring.
    """
    _request(name, value)
    fields = {} if fields is None else fields
    if name in SHORT_SWITCHES:
        sub, on, off = SHORT_SWITCHES[name]
        return _finish(0x1F, [sub, on if value else off])
    if not isinstance(fa, bytes) or not valid_frame(fa):
        raise ValueError("A complete checksum-valid fresh FA frame is required")
    if fa[3] != 0x21 or fa[5] != 0xFA or len(fa[6:-2]) < 8:
        raise ValueError("Expected toilet FA status with at least 8 payload bytes")
    woman = fields.get("is_woman", False)
    if type(woman) is not bool:
        raise ValueError("is_woman must be bool")
    if name in ("wash_woman", "wash_auto_woman"):
        woman = True
    elif name in ("wash_hip", "wash_auto_hip", "stop", "flush_large", "flush_small",
                  "lid_open", "lid_close", "ring_open", "ring_close"):
        woman = False
    water = _level((fa[6] >> 4) & 7, 0, 5, "baseline water")
    seat = _level(fa[7] & 7, 0, 5, "baseline seat")
    air = _level(fa[8] & 7, 0, 3, "baseline air")
    nozzle = (fa[9] & 15) if woman else ((fa[7] >> 4) & 7)
    pressure = (fa[9] >> 4) if woman else ((fa[8] >> 4) & 7)
    _level(nozzle, 1, 5, "baseline nozzle")
    _level(pressure, 1, 5, "baseline pressure")
    width = _level(fa[13] & 7, 1, 7, "baseline wash width")
    # Preserve raw bits 0/2, not FA[6].7 (a dynamic unblock status bit).
    b6 = fa[6] & 0x7F
    b7 = (fa[7] & 0x80) | (nozzle << 4) | seat
    b8 = (fa[8] & 0x80) | (pressure << 4) | air
    if name == "water_level":
        b6 = (b6 & ~0x70) | (value << 4)
    elif name == "seat_level":
        b7 = (b7 & ~7) | value
    elif name == "air_level":
        b8 = (b8 & ~7) | value
    code = 25 if name in LEVELS else ACTIONS[name]
    if name == "lid_open":
        code = 19 if _ring(fields) else 17
    # Width is FA[13].0..2 -> TX[9].4..6; wind FA[10] -> TX[11].
    # TX[10], timer bytes [12:16], custom-mode/user byte [16] are source-zero.
    return _finish(code, [b6, b7, b8, width << 4, 0, fa[10] & 0x70, 0, 0, 0, 0, 0])


def expected_after(name: str, value: object = None, fields: dict | None = None) -> dict:
    """Return only source-backed readback expectations, not optimistic state.

    {} means no generic completion predicate: the caller may correlate E9 work
    state, but must not claim success simply because a transport write completed.
    """
    _request(name, value)
    fields = {} if fields is None else fields
    if name in SHORT_SWITCHES or name in LEVELS:
        return {name: value}
    if name == "lid_close":
        return {"lid_open": False, "ring_open": False}
    if name == "lid_open":
        return {"lid_open": True, "ring_open": _ring(fields)}
    if name == "ring_open":
        return {"lid_open": True, "ring_open": True}
    if name == "ring_close":
        return {"lid_open": True, "ring_open": False}
    return {}
