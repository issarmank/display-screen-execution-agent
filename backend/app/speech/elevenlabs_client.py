"""Thin wrapper around the ElevenLabs Scribe v2 Realtime SDK connection.

SDK surface (elevenlabs 2.69): ``ScribeRealtime.connect(options)`` returns a
``RealtimeConnection`` with ``on(event, cb)``, ``send({"audio_base_64": ...})``,
``commit()`` and ``close()``. Server messages are delivered to callbacks as raw
dicts keyed by ``message_type``.
"""

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from elevenlabs.realtime import (
    AudioFormat,
    CommitStrategy,
    RealtimeConnection,
    RealtimeEvents,
    ScribeRealtime,
)

MODEL_ID = "scribe_v2_realtime"
DEFAULT_BASE_URL = "wss://api.elevenlabs.io"
SAMPLE_RATE = 16000

PartialCallback = Callable[[str], None]
CommittedCallback = Callable[["CommittedSegment"], None]
ErrorCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class CommittedSegment:
    """A stable transcript segment. Offsets are seconds from the start of the audio stream."""

    text: str
    start_s: float | None = None
    end_s: float | None = None


def segment_from_payload(payload: dict[str, Any]) -> CommittedSegment:
    """Build a segment from a committed_transcript[_with_timestamps] message."""
    text = str(payload.get("text") or "").strip()
    timed = [
        w
        for w in payload.get("words") or []
        if isinstance(w, dict)
        and w.get("type", "word") == "word"
        and w.get("start") is not None
        and w.get("end") is not None
    ]
    if not timed:
        return CommittedSegment(text=text)
    return CommittedSegment(
        text=text,
        start_s=float(min(w["start"] for w in timed)),
        end_s=float(max(w["end"] for w in timed)),
    )


class RealtimeTranscriber:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        include_timestamps: bool = True,
        language_code: str | None = None,
        scribe: ScribeRealtime | None = None,
    ) -> None:
        if not api_key and scribe is None:
            raise ValueError("ElevenLabs API key is required (set ELEVEN_LABS_API_KEY)")
        self._scribe = scribe or ScribeRealtime(api_key=api_key, base_url=base_url)
        self._include_timestamps = include_timestamps
        self._language_code = language_code
        self._connection: RealtimeConnection | None = None
        self._partial_cbs: list[PartialCallback] = []
        self._committed_cbs: list[CommittedCallback] = []
        self._error_cbs: list[ErrorCallback] = []
        self._commit_seen = asyncio.Event()
        self._stopping = False
        self._flushing = False
        self.session_started = asyncio.Event()
        self.closed = asyncio.Event()

    def on_partial(self, cb: PartialCallback) -> None:
        self._partial_cbs.append(cb)

    def on_committed(self, cb: CommittedCallback) -> None:
        self._committed_cbs.append(cb)

    def on_error(self, cb: ErrorCallback) -> None:
        self._error_cbs.append(cb)

    @property
    def is_open(self) -> bool:
        return self._connection is not None and not self.closed.is_set()

    async def start(self) -> None:
        if self._connection is not None:
            raise RuntimeError("Transcriber already started")
        # Fresh per-connection state so a stopped transcriber can be started again.
        self._stopping = False
        self._commit_seen.clear()
        self.session_started.clear()
        self.closed.clear()
        options: dict[str, Any] = {
            "model_id": MODEL_ID,
            "audio_format": AudioFormat.PCM_16000,
            "sample_rate": SAMPLE_RATE,
            # Server-side VAD commits a segment when the speaker pauses.
            "commit_strategy": CommitStrategy.VAD,
            "include_timestamps": self._include_timestamps,
        }
        if self._language_code:
            options["language_code"] = self._language_code
        conn = await self._scribe.connect(options)  # type: ignore[call-overload]
        self._connection = conn

        conn.on(RealtimeEvents.SESSION_STARTED, lambda _data: self.session_started.set())
        conn.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self._handle_partial)
        # With timestamps on, the server sends both committed events for each
        # segment; subscribe to exactly one so each segment is delivered once.
        committed_event = (
            RealtimeEvents.COMMITTED_TRANSCRIPT_WITH_TIMESTAMPS
            if self._include_timestamps
            else RealtimeEvents.COMMITTED_TRANSCRIPT
        )
        conn.on(committed_event, self._handle_committed)
        conn.on(RealtimeEvents.ERROR, self._handle_error)
        conn.on(RealtimeEvents.CLOSE, lambda *_: self.closed.set())

    async def send_chunk(self, pcm: bytes) -> None:
        """Send raw 16 kHz mono PCM16 little-endian audio."""
        if self._connection is None:
            raise RuntimeError("Transcriber not started")
        if not pcm:
            return
        await self._connection.send({"audio_base_64": base64.b64encode(pcm).decode("ascii")})

    async def flush(self, timeout: float = 2.0) -> bool:
        """Force-commit buffered audio and wait for the resulting segment.

        Returns True if the server answered the commit before the timeout: either with a
        committed segment, or by saying there was too little uncommitted audio to commit.
        """
        if not self.is_open or self._connection is None:
            return False
        self._commit_seen.clear()
        self._flushing = True
        try:
            await self._connection.commit()
            await asyncio.wait_for(self._commit_seen.wait(), timeout)
        except TimeoutError:
            return False
        finally:
            self._flushing = False
        return True

    async def stop(self) -> None:
        if self._connection is None:
            return
        conn, self._connection = self._connection, None
        self._stopping = True
        await conn.close()
        self.closed.set()

    def _handle_partial(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        for cb in self._partial_cbs:
            cb(text)

    def _handle_committed(self, payload: dict[str, Any]) -> None:
        segment = segment_from_payload(payload)
        self._commit_seen.set()
        if not segment.text:
            return
        for cb in self._committed_cbs:
            cb(segment)

    def _handle_error(self, payload: dict[str, Any]) -> None:
        # Closing the socket ourselves makes the SDK report the close handshake
        # (e.g. "no close frame received") as an error; that isn't a real failure.
        if self._stopping:
            return
        # Flushing right after VAD already committed leaves < 0.3 s of audio, which Scribe
        # rejects with commit_throttled. For a flush that just means nothing was left.
        if self._flushing and payload.get("message_type") == "commit_throttled":
            self._commit_seen.set()
            return
        for cb in self._error_cbs:
            cb(payload)
