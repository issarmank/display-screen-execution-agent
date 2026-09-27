"""Manual harness for the voice WebSocket: mic (or a WAV file) -> FastAPI -> ElevenLabs.

Usage (from backend/, with `uvicorn app.main:app` running):
    python scripts/ws_mic_client.py                    # new session, stream the mic
    python scripts/ws_mic_client.py --session <id>     # attach to an existing session
    python scripts/ws_mic_client.py --wav clip.wav     # stream a 16 kHz mono PCM16 WAV

Speak, pause to let VAD commit a segment, Ctrl+C to send "stop" and wait for "done".
"""

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import urllib.request
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.speech.audio_source import CHUNK_MS, MicrophoneSource  # noqa: E402
from app.speech.elevenlabs_client import SAMPLE_RATE  # noqa: E402

CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2


def create_session(base_url: str) -> str:
    req = urllib.request.Request(f"{base_url}/sessions", method="POST")
    with urllib.request.urlopen(req) as res:
        body: dict[str, Any] = json.load(res)
    return str(body["id"])


async def wav_chunks(path: Path) -> AsyncIterator[bytes]:
    """Yield a WAV file in 100 ms chunks at real-time pace, then 1.5 s of silence."""
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise SystemExit(f"{path} must be {SAMPLE_RATE} Hz mono 16-bit PCM")
        data = wav.readframes(wav.getnframes())
    data += b"\x00" * (CHUNK_BYTES * 15)  # trailing silence lets server VAD commit
    for i in range(0, len(data), CHUNK_BYTES):
        yield data[i : i + CHUNK_BYTES]
        await asyncio.sleep(CHUNK_MS / 1000)


def show(event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "partial":
        print(f"\r\033[K… {event['text']}", end="", flush=True)
    elif kind == "turn":
        turn = event["turn"]
        print(f"\r\033[K✔ [{turn['source']}] {turn['text']}", flush=True)
    elif kind == "error":
        tag = "FATAL" if event.get("fatal") else "error"
        print(f"\r\033[K! {tag} {event['code']}: {event['message']}", file=sys.stderr, flush=True)
    else:
        print(f"\r\033[K{json.dumps(event)}", flush=True)


async def pump(ws: ClientConnection, chunks: AsyncIterator[bytes]) -> None:
    async for chunk in chunks:
        await ws.send(chunk)


async def main(base_url: str, session_id: str | None, wav: Path | None, device: Any) -> int:
    session_id = session_id or create_session(base_url)
    ws_url = base_url.replace("http", "ws", 1) + f"/sessions/{session_id}/voice"
    print(f"Session {session_id}\nConnecting to {ws_url} (Ctrl+C to stop)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    async with contextlib.AsyncExitStack() as stack:
        ws = await stack.enter_async_context(connect(ws_url))
        first = json.loads(await ws.recv())
        show(first)
        if first.get("type") != "ready":
            return 1

        if wav is not None:
            source: AsyncIterator[bytes] = wav_chunks(wav)
        else:
            mic = await stack.enter_async_context(MicrophoneSource(device=device))
            source = mic.chunks()
        sender = asyncio.create_task(pump(ws, source))
        # A WAV run stops itself once the file (plus trailing silence) has been sent.
        sender.add_done_callback(lambda _t: stop.set())

        async def stop_when_asked() -> None:
            await stop.wait()
            sender.cancel()
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps({"type": "stop"}))

        stopper = asyncio.create_task(stop_when_asked())
        try:
            async for raw in ws:
                show(json.loads(raw))
        except ConnectionClosed:
            pass
        stopper.cancel()
        sender.cancel()
        print(f"Closed: {ws.close_code} {ws.close_reason or ''}".rstrip())
        return 0 if ws.close_code == 1000 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="backend base URL")
    parser.add_argument("--session", default=None, help="existing session id to attach to")
    parser.add_argument("--wav", type=Path, default=None, help="stream this WAV instead of mic")
    parser.add_argument("--device", default=None, help="sounddevice input device index/name")
    args = parser.parse_args()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    sys.exit(asyncio.run(main(args.url.rstrip("/"), args.session, args.wav, device)))
