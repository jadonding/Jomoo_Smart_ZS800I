"""Connection-local notification state, without HA or persistent-cache dependencies."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import monotonic

from .protocol import FrameParser, status_command

_LOGGER = logging.getLogger(__name__)
CLOSE_TIMEOUT = 3.0


class BLESession:
    """Own one notification subscription; caller owns connection and serialization."""

    def __init__(self, client, char, on_frame: Callable[[bytes], None]) -> None:
        self.client = client
        self.char = char
        self.on_frame = on_frame
        self.active = False
        self.frames: dict[str, bytes] = {}
        self.frame_times: dict[str, float] = {}
        self.last_rx_at = 0.0
        self.last_write_at = 0.0
        self._parser = FrameParser()
        self._observers: list[Callable[[bytes], None]] = []
        self._started = False
        self._closed = False

    def _invalidate(self) -> None:
        self.active = False
        self.frames.clear()
        self.frame_times.clear()
        self._parser.reset()
        self._observers.clear()

    def invalidate(self) -> None:
        """Invalidate immediately on an explicit backend disconnect callback."""
        self._invalidate()

    @property
    def live(self) -> bool:
        connected = bool(self.client.is_connected)
        if self.active and not connected:
            self._invalidate()
        return self.active and connected

    def add_observer(self, callback: Callable[[bytes], None]) -> Callable[[], None]:
        """Register before start to include notifications emitted during subscription."""
        if self._closed:
            raise RuntimeError("Session is closed")
        self._observers.append(callback)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                try:
                    self._observers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Session is closed")
        if self._started:
            return
        self._started = True
        self.active = True
        try:
            await self.client.start_notify(self.char, self._notify)
        except BaseException:
            self._invalidate()
            raise

    @staticmethod
    def _dispatch(callback, frame: bytes) -> None:
        try:
            callback(frame)
        except Exception:
            _LOGGER.exception("BLE session frame callback failed")

    def _notify(self, _sender, data: bytearray) -> None:
        if not self.live:
            return
        self.last_rx_at = monotonic()
        for frame in self._parser.feed(data):
            if not self.live:
                break
            command = status_command(frame)
            if command is None or frame[3] != 0x21:
                continue
            key = f"{command:02X}"
            self.frames[key] = frame
            self.frame_times[key] = monotonic()
            self._dispatch(self.on_frame, frame)
            for observer in tuple(self._observers):
                if not self.live:
                    break
                self._dispatch(observer, frame)

    async def write(self, data: bytes, *, response: bool) -> None:
        """Track transport write completion for command pacing, without retries."""
        try:
            await self.client.write_gatt_char(self.char, data, response=response)
        finally:
            self.last_write_at = monotonic()

    def reset(self) -> None:
        """Drop a partial transport frame without discarding live session state."""
        self._parser.reset()

    async def close(self) -> None:
        """Invalidate first; bound both cleanup operations and always try disconnect."""
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        try:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self.client.stop_notify(self.char)
            except Exception:
                _LOGGER.debug("BLE session stop_notify failed", exc_info=True)
        finally:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self.client.disconnect()
            except Exception:
                _LOGGER.debug("BLE session disconnect failed", exc_info=True)
