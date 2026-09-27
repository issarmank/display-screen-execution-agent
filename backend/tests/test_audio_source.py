import asyncio
import sys
import types
from typing import Any

import pytest

from app.speech.audio_source import MicrophoneSource


class FakeStream:
    instances: list["FakeStream"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = self.closed = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> type[FakeStream]:
    FakeStream.instances = []
    module = types.ModuleType("sounddevice")
    module.RawInputStream = FakeStream  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return FakeStream


async def test_mic_opens_16k_mono_int16_and_yields_chunks(
    fake_sounddevice: type[FakeStream],
) -> None:
    status_ok = types.SimpleNamespace(input_overflow=False)
    status_overflow = types.SimpleNamespace(input_overflow=True)

    async with MicrophoneSource(chunk_ms=100) as mic:
        stream = fake_sounddevice.instances[0]
        assert stream.started
        assert stream.kwargs["samplerate"] == 16000
        assert stream.kwargs["channels"] == 1
        assert stream.kwargs["dtype"] == "int16"
        assert stream.kwargs["blocksize"] == 1600
        callback = stream.kwargs["callback"]
        # PortAudio calls back from its own thread.
        await asyncio.to_thread(callback, b"\x01\x02", 1, None, status_ok)
        await asyncio.to_thread(callback, b"\x03\x04", 1, None, status_overflow)

    received = [c async for c in mic.chunks()]
    assert received == [b"\x01\x02", b"\x03\x04"]
    assert mic.overflows == 1
    assert stream.closed


async def test_mic_exit_is_idempotent(fake_sounddevice: type[FakeStream]) -> None:
    async with MicrophoneSource() as mic:
        await mic.__aexit__(None, None, None)
    assert [c async for c in mic.chunks()] == []
