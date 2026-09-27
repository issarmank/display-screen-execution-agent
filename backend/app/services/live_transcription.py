"""Glue between an audio source, the realtime transcriber and the DB service."""

import asyncio
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import CommittedSegment, RealtimeTranscriber

FATAL_ERRORS = {
    "auth_error",
    "quota_exceeded",
    "unaccepted_terms",
    "invalid_request",
    "session_time_limit_exceeded",
}


@dataclass
class LiveSessionResult:
    session_id: str
    turn_count: int
    errors: list[dict[str, Any]] = field(default_factory=list)


class FatalTranscriptionError(RuntimeError):
    pass


async def run_live_session(
    transcriber: RealtimeTranscriber,
    service: TranscriptionService,
    audio: AsyncIterable[bytes],
    *,
    on_partial: Callable[[str], None] = lambda _t: None,
    on_turn: Callable[[CommittedSegment], None] = lambda _s: None,
    on_error: Callable[[dict[str, Any]], None] = lambda _e: None,
    flush_timeout: float = 2.0,
    start_new_session: bool = True,
    end_session_on_exit: bool = True,
) -> LiveSessionResult:
    """Stream ``audio`` until it ends (or the task is cancelled), persisting committed turns.

    By default a fresh session row is created and always closed out, even on
    cancellation or error. Callers that manage the session themselves (the voice
    WebSocket attaches to an existing conversation) pass ``start_new_session=False``
    after ``service.attach_session()`` and ``end_session_on_exit=False`` so the
    conversation outlives this one voice stream.
    """
    errors: list[dict[str, Any]] = []
    fatal = asyncio.Event()

    def handle_committed(segment: CommittedSegment) -> None:
        if service.record_committed(segment) is not None:
            on_turn(segment)

    def handle_error(payload: dict[str, Any]) -> None:
        errors.append(payload)
        on_error(payload)
        if payload.get("message_type") in FATAL_ERRORS:
            fatal.set()

    transcriber.on_partial(on_partial)
    transcriber.on_committed(handle_committed)
    transcriber.on_error(handle_error)

    if start_new_session:
        service.start_session()
    try:
        await transcriber.start()
        service.mark_audio_started()
        async for chunk in audio:
            if fatal.is_set() or transcriber.closed.is_set():
                break
            await transcriber.send_chunk(chunk)
        if fatal.is_set():
            raise FatalTranscriptionError(errors[-1])
        await transcriber.flush(flush_timeout)
    finally:
        # Shield cleanup so Ctrl+C mid-stream still closes the socket, and close the
        # session row even if a second cancellation interrupts the socket close.
        try:
            await asyncio.shield(transcriber.stop())
        finally:
            turn_count = service.end_session() if end_session_on_exit else service.turn_count()
    return LiveSessionResult(session_id=service.session_id, turn_count=turn_count, errors=errors)
