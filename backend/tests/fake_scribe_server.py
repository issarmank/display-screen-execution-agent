"""A local stand-in for wss://api.elevenlabs.io/v1/speech-to-text/realtime.

Runs the real SDK against a scripted server so tests are deterministic and offline.
"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve


@dataclass
class ServerLog:
    path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)


def committed(
    text: str, words: list[tuple[str, float, float]] | None = None
) -> list[dict[str, Any]]:
    """The pair of messages Scribe sends for a segment when include_timestamps is on."""
    return [
        {"message_type": "committed_transcript", "text": text},
        {
            "message_type": "committed_transcript_with_timestamps",
            "text": text,
            "language_code": "en",
            "words": [
                {"text": w, "start": s, "end": e, "type": "word"} for w, s, e in (words or [])
            ],
        },
    ]


CLOSE: dict[str, Any] = {"__close__": True}

# script(message, index_of_audio_chunk) -> list of server messages to send back
Script = Callable[[dict[str, Any], int], list[dict[str, Any]]]


@asynccontextmanager
async def fake_scribe(
    script: Script, *, greeting: Sequence[dict[str, Any] | str] | None = None
) -> AsyncIterator[tuple[str, ServerLog]]:
    log = ServerLog()

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        log.path = ws.request.path
        log.headers = dict(ws.request.headers.raw_items())
        for msg in (
            greeting
            if greeting is not None
            else [{"message_type": "session_started", "session_id": "fake", "config": {}}]
        ):
            await ws.send(msg if isinstance(msg, str) else json.dumps(msg))  # str = malformed
        idx = 0
        async for raw in ws:
            log.raw.append(str(raw))
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            log.messages.append(msg)
            for reply in script(msg, idx):
                if reply == CLOSE:
                    await ws.close()
                    return
                await ws.send(json.dumps(reply))
            idx += 1

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}", log
