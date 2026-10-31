"""Pure-Python bounded F3F4 framing; no BLE or Home Assistant imports."""
MIN_FRAME = 8
MAX_FRAME = 259  # one unsigned length byte plus header/checksum/trailer
MAX_INPUT = 4096
MAX_BUFFER = MAX_FRAME * 2
STATUS_COMMANDS = frozenset((0xFA, 0xE9, 0xEA))


def valid_frame(frame: bytes) -> bool:
    """Validate exact frame size, header, checksum and trailer."""
    return (
        MIN_FRAME <= len(frame) <= MAX_FRAME
        and frame[:2] == b"\xf3\xf4"
        and frame[2] >= MIN_FRAME - 4
        and len(frame) == frame[2] + 4
        and frame[-1] == 0xFC
        and (sum(frame[2:-2]) & 0xFF) == frame[-2]
    )


def status_command(frame: bytes) -> int | None:
    if valid_frame(frame) and frame[5] in STATUS_COMMANDS:
        return frame[5]
    return None


class FrameParser:
    """Handle split/coalesced frames; discard corrupt bytes and oversized input.

    Never accumulate an unbounded notification. A corrupt length can delay
    resynchronization until at most MAX_FRAME bytes have arrived.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        """Discard an incomplete frame at a transaction phase boundary."""
        self._buffer.clear()

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        if len(data) > MAX_INPUT:
            self._buffer.clear()
            return []
        frames = []
        # Process incrementally so even one MAX_INPUT chunk stays bounded.
        for value in data:
            self._buffer.append(value)
            while len(self._buffer) >= 2:
                if self._buffer[:2] != b"\xf3\xf4":
                    del self._buffer[0]
                    continue
                if len(self._buffer) < 3:
                    break
                size = self._buffer[2] + 4
                if size < MIN_FRAME:
                    del self._buffer[0]
                    continue
                if len(self._buffer) < size:
                    # A corrupt length must not hide a complete valid frame.
                    # Resynchronize only on a fully validated nested header.
                    offset = self._buffer.find(b"\xf3\xf4", 2)
                    recovered = False
                    while offset >= 0 and offset + 3 <= len(self._buffer):
                        nested_size = self._buffer[offset + 2] + 4
                        end = offset + nested_size
                        if end <= len(self._buffer) and valid_frame(bytes(self._buffer[offset:end])):
                            del self._buffer[:offset]
                            recovered = True
                            break
                        offset = self._buffer.find(b"\xf3\xf4", offset + 1)
                    if recovered:
                        continue
                    break
                candidate = bytes(self._buffer[:size])
                if valid_frame(candidate):
                    frames.append(candidate)
                    del self._buffer[:size]
                else:
                    del self._buffer[0]
        return frames


def decode_source_fields(frame: bytes) -> dict[str, int | bool]:
    """Source-derived semantics, pending real-device operation correlation.

    Only payload bytes are indexed. In particular the FC trailer is NEVER a
    state byte. 237371 cover/ring state uses EA, not E9/E2.
    """
    command = status_command(frame)
    if command is None:
        return {}
    payload = frame[6:-2]
    if command == 0xFA and len(payload) >= 3:
        result = {"water_level": (payload[0] >> 4) & 7,
                  "seat_level": payload[1] & 7, "air_level": payload[2] & 7}
        if len(payload) >= 5:
            result.update(close_flush=bool(payload[4] & 2),
                          flush_small_running=bool(payload[4] & 4),
                          flush_large_running=bool(payload[4] & 8))
        if len(payload) >= 6:
            result.update(atmosphere_on=bool(payload[5] & 0x10),
                          uv_water=bool(payload[5] & 0x20),
                          # FA[11].bit3 is the seasonal-auto temperature mode
                          # (upstream dealWithFA); not part of the 19-byte body.
                          auto_temp=bool(payload[5] & 8))
        if len(payload) >= 7:
            result.update(auto_bubble=bool(payload[6] & 8), prewet=bool(payload[6] & 0x20))
        if len(payload) >= 8:
            result.update(foot_sensor=bool(payload[7] & 0x20),
                          auto_small_flush=bool(payload[7] & 0x10))
        return result
    if command == 0xE9 and payload:
        result = {"occupancy": bool(payload[0] & 0x80)}
        if len(payload) >= 4:
            code = payload[3]
            result.update(work_code=code, working=code not in (1, 2), dry_running=code in (16, 144))
            if len(payload) >= 5:
                flags = payload[4]
                special = code in (1, 2, 3, 131, 16, 144) or bool(flags & 0x24)
                hip = not special and bool(code & 0x20)
                woman = not special and not hip and bool(code & 0x40)
                result.update(auto_work=bool(flags & 8), hip_wash_running=hip,
                              woman_wash_running=woman, is_woman=woman)
        return result
    if command == 0xEA and payload:
        return {"ring_open": bool(payload[0] & 0x02),
                "lid_open": bool(payload[0] & 0x08),
                "ring_moving": bool(payload[0] & 0x20),
                "lid_moving": bool(payload[0] & 0x40)}
    return {}
