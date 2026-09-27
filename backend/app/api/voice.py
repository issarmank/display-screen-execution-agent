"""WebSocket ``/sessions/{id}/voice``: relay client mic audio to ElevenLabs Scribe.

Client -> server:
  * binary frames: raw PCM16 little-endian, 16 kHz mono (~100 ms each)
  * text frames: JSON control messages; only ``{"type": "stop"}`` for now

Server -> client (JSON text frames):
  * ``{"type": "ready"}`` once the upstream transcriber is connected
  * ``{"type": "partial", "text": str}``
  * ``{"type": "turn", "turn": TurnOut}`` for every committed, persisted segment
  * ``{"type": "error", "code": str, "message": str, "fatal": bool}``
  * ``{"type": "done", "turn_count": int}`` after stop/flush, then close 1000

Close codes: 4404 unknown session, 4409 session not active (at connect, or ended via
REST while streaming: turns committed after that are refused), 4429 a voice stream is
already running for this session, 1011 fatal upstream/transcriber failure.

Half-open clients are detected by uvicorn's WebSocket keepalive pings (20 s by default).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, WebSocket
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketState

from app.api.schemas import TurnOut
from app.models import Turn
from app.services.live_transcription import FATAL_ERRORS, FatalTranscriptionError, run_live_session
from app.services.session_service import SessionClosed, SessionNotFound, SessionService
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import CommittedSegment

logger = logging.getLogger(__name__)
router = APIRouter()

CLOSE_NORMAL = 1000
CLOSE_INTERNAL_ERROR = 1011
CLOSE_NOT_FOUND = 4404
CLOSE_NOT_ACTIVE = 4409
CLOSE_BUSY = 4429

# ~60 s of 100 ms chunks buffered while the upstream socket catches up.
MAX_BUFFERED_CHUNKS = 600

Event = dict[str, Any]


def ready_event() -> Event:
    return {"type": "ready"}


def partial_event(text: str) -> Event:
    return {"type": "partial", "text": text}


def turn_event(turn: Turn) -> Event:
    return {"type": "turn", "turn": TurnOut.model_validate(turn).model_dump(mode="json")}


def error_event(code: str, message: str, *, fatal: bool = False) -> Event:
    return {"type": "error", "code": code, "message": message, "fatal": fatal}


def done_event(turn_count: int) -> Event:
    return {"type": "done", "turn_count": turn_count}


class _NotifyingTranscriptionService(TranscriptionService):
    """Reports each persisted turn so the socket can echo the stored row, and reports
    (once) when the conversation was ended elsewhere so the stream can stop."""

    def __init__(
        self,
        session_factory: sessionmaker[Any],
        on_turn: Callable[[Turn], None],
        on_session_closed: Callable[[], None],
    ) -> None:
        super().__init__(session_factory)
        self._on_turn = on_turn
        self._on_session_closed = on_session_closed
        self.recorded = 0

    def record_committed(self, segment: CommittedSegment) -> Turn | None:
        already_closed = self.session_closed
        turn = super().record_committed(segment)
        if turn is not None:
            self.recorded += 1
            self._on_turn(turn)
        elif self.session_closed and not already_closed:
            self._on_session_closed()
        return turn


class _VoiceStream:
    def __init__(self, websocket: WebSocket, session_id: str) -> None:
        self.ws = websocket
        self.session_id = session_id
        self.outbox: asyncio.Queue[Event | None] = asyncio.Queue()
        self.audio: asyncio.Queue[bytes | None] = asyncio.Queue(MAX_BUFFERED_CHUNKS)
        self.stop_requested = False
        self.client_gone = False
        self.session_closed = False

    def emit(self, event: Event) -> None:
        # Transcriber callbacks are sync; they only enqueue, never await the socket.
        self.outbox.put_nowait(event)

    def end_audio(self) -> None:
        # Unblocks the audio generator even if the buffer is full.
        while True:
            try:
                self.audio.put_nowait(None)
                return
            except asyncio.QueueFull:
                self.audio.get_nowait()

    async def send_loop(self) -> None:
        while (event := await self.outbox.get()) is not None:
            if self.client_gone:
                continue
            try:
                await self.ws.send_json(event)
            except Exception:  # client vanished mid-send
                self.client_gone = True

    async def receive_loop(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                self.client_gone = True
                self.end_audio()
                return
            data = message.get("bytes")
            if data is not None:
                self._handle_audio(data)
            elif message.get("text") is not None:
                self._handle_control(message["text"])

    def _handle_audio(self, data: bytes) -> None:
        if self.stop_requested or not data:
            return
        if len(data) % 2:
            self.emit(error_event("bad_audio", "PCM16 frames must have an even byte length"))
            return
        try:
            self.audio.put_nowait(data)
        except asyncio.QueueFull:
            self.emit(error_event("backpressure", "Audio buffer full; chunk dropped"))

    def _handle_control(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self.emit(error_event("bad_json", "Control message is not valid JSON"))
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "stop":
            if not self.stop_requested:
                self.stop_requested = True
                self.end_audio()
            return
        self.emit(error_event("unknown_type", f"Unsupported control message type: {kind!r}"))

    async def audio_chunks(self) -> AsyncIterator[bytes]:
        # run_live_session only starts pulling audio once the transcriber is connected.
        self.emit(ready_event())
        while (chunk := await self.audio.get()) is not None:
            yield chunk

    def on_session_closed(self) -> None:
        self.session_closed = True
        self.emit(
            error_event("session_closed", "The session was ended; voice input stopped", fatal=True)
        )
        self.end_audio()

    def on_upstream_error(self, payload: dict[str, Any]) -> None:
        code = str(payload.get("message_type") or "upstream_error")
        message = str(payload.get("error") or payload.get("message") or code)
        fatal = code in FATAL_ERRORS
        logger.warning("Transcriber error on session %s: %s: %s", self.session_id, code, message)
        self.emit(error_event(code, message, fatal=fatal))
        if fatal:
            self.end_audio()  # don't wait for the next client chunk to notice


@router.websocket("/sessions/{session_id}/voice")
async def voice_stream(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    state = websocket.app.state
    sessions: SessionService = state.session_service
    try:
        sessions.ensure_active(session_id)
    except SessionNotFound:
        await websocket.close(CLOSE_NOT_FOUND, "Session not found")
        return
    except SessionClosed:
        await websocket.close(CLOSE_NOT_ACTIVE, "Session is not active")
        return

    registry = state.voice_registry
    if not await registry.claim(session_id):
        await websocket.close(CLOSE_BUSY, "A voice stream is already active for this session")
        return
    try:
        close_code = await _run(websocket, session_id)
    finally:
        await registry.release(session_id)
    if websocket.client_state == WebSocketState.CONNECTED:
        with contextlib.suppress(Exception):
            await websocket.close(close_code)


async def _run(websocket: WebSocket, session_id: str) -> int:
    state = websocket.app.state
    stream = _VoiceStream(websocket, session_id)
    service = _NotifyingTranscriptionService(
        state.session_factory,
        on_turn=lambda turn: stream.emit(turn_event(turn)),
        on_session_closed=stream.on_session_closed,
    )
    sender = asyncio.create_task(stream.send_loop())
    reader = asyncio.create_task(stream.receive_loop())
    watcher: asyncio.Task[None] | None = None
    close_code = CLOSE_NORMAL
    try:
        # Inside the try so an unexpected DB failure still gets an error event + 1011.
        try:
            service.attach_session(session_id)
        except SessionNotFound:  # deleted between the check above and now
            return CLOSE_NOT_FOUND
        except SessionClosed:  # ended between the check above and now
            return CLOSE_NOT_ACTIVE
        transcriber = state.transcriber_factory()

        async def end_audio_when_upstream_closes() -> None:
            await transcriber.closed.wait()
            stream.end_audio()

        watcher = asyncio.create_task(end_audio_when_upstream_closes())
        await run_live_session(
            transcriber,
            service,
            stream.audio_chunks(),
            on_partial=lambda text: stream.emit(partial_event(text)),
            on_error=stream.on_upstream_error,
            start_new_session=False,
            end_session_on_exit=False,
        )
        if stream.session_closed:
            close_code = CLOSE_NOT_ACTIVE  # the session_closed error was already emitted
        elif stream.stop_requested or stream.client_gone:
            stream.emit(done_event(service.recorded))
        else:
            # The audio only ends on stop/disconnect, so this was the upstream closing on us.
            stream.emit(error_event("upstream_closed", "Transcriber closed", fatal=True))
            close_code = CLOSE_INTERNAL_ERROR
    except FatalTranscriptionError:
        close_code = CLOSE_INTERNAL_ERROR  # the fatal error event was already emitted
    except Exception as exc:
        logger.exception("Voice stream for session %s failed", session_id)
        stream.emit(error_event("upstream_unavailable", str(exc) or type(exc).__name__, fatal=True))
        close_code = CLOSE_INTERNAL_ERROR
    finally:
        for task in (reader, watcher):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        stream.outbox.put_nowait(None)
        await sender
    return close_code
