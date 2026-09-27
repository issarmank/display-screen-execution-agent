"""Microphone capture: 16 kHz mono PCM16 chunks delivered to an asyncio queue."""

import asyncio
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

from app.speech.elevenlabs_client import SAMPLE_RATE

CHUNK_MS = 100


class MicrophoneSource:
    def __init__(self, *, chunk_ms: int = CHUNK_MS, device: int | str | None = None) -> None:
        self._blocksize = SAMPLE_RATE * chunk_ms // 1000
        self._device = device
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self.overflows = 0

    def _callback(self, indata: Any, _frames: int, _time: Any, status: Any) -> None:
        # Runs on PortAudio's thread; hand the bytes to the event loop thread-safely.
        if status and status.input_overflow:
            self.overflows += 1
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._queue.put_nowait, bytes(indata))

    async def __aenter__(self) -> "MicrophoneSource":
        import sounddevice as sd  # imported lazily so tests never need PortAudio

        self._loop = asyncio.get_running_loop()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._queue.put_nowait(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk
