import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn
from app.services.live_transcription import FatalTranscriptionError, run_live_session
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import RealtimeTranscriber
from tests.conftest import FakeClock
from tests.fake_scribe_server import committed, fake_scribe


async def chunks(n: int, *, gap: float = 0.0) -> AsyncIterator[bytes]:
    for _ in range(n):
        yield b"\x00\x01" * 1600
        await asyncio.sleep(gap)


def only_session(session_factory: sessionmaker[Any]) -> tuple[Session, list[Turn]]:
    with session_factory() as db:
        (session,) = db.scalars(select(Session)).all()
        turns = list(db.scalars(select(Turn).order_by(Turn.created_at)).all())
    return session, turns


async def test_streams_audio_persists_turns_and_completes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        if msg.get("commit"):
            return committed("and a trailing thought", [("and", 2.0, 2.2), ("thought", 2.5, 2.9)])
        if idx == 0:
            return [{"message_type": "partial_transcript", "text": "open"}]
        if idx == 1:
            return committed("open the browser", [("open", 0.1, 0.3), ("browser", 0.6, 1.0)])
        return []

    partials: list[str] = []
    async with fake_scribe(script) as (url, _log):
        result = await run_live_session(
            RealtimeTranscriber("k", base_url=url),
            TranscriptionService(session_factory, clock=clock),
            chunks(3, gap=0.05),
            on_partial=partials.append,
        )

    assert result.turn_count == 2 and result.errors == []
    session, turns = only_session(session_factory)
    assert session.id == result.session_id
    assert session.status == SessionStatus.COMPLETED and session.ended_at is not None
    assert [t.text for t in turns] == ["open the browser", "and a trailing thought"]
    assert all(t.session_id == session.id for t in turns)
    assert turns[0].started_at is not None and turns[0].started_at < turns[1].started_at  # type: ignore[operator]
    assert partials == ["open"]


async def test_fatal_server_error_raises_but_still_closes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    greeting = [{"message_type": "auth_error", "error": "invalid api key"}]
    async with fake_scribe(lambda _m, _i: [], greeting=greeting) as (url, _log):
        with pytest.raises(FatalTranscriptionError, match="invalid api key"):
            await run_live_session(
                RealtimeTranscriber("bad", base_url=url),
                TranscriptionService(session_factory, clock=clock),
                chunks(20, gap=0.02),
            )

    session, turns = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED and turns == []


async def test_non_fatal_error_is_reported_and_session_continues(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    def script(_m: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        if idx == 0:
            return [{"message_type": "rate_limited", "error": "slow down"}]
        return committed("made it") if idx == 1 else []

    async with fake_scribe(script) as (url, _log):
        result = await run_live_session(
            RealtimeTranscriber("k", base_url=url),
            TranscriptionService(session_factory, clock=clock),
            chunks(3, gap=0.05),
            flush_timeout=0.1,
        )
    assert [e["message_type"] for e in result.errors] == ["rate_limited"]
    assert result.turn_count == 1


async def test_cancellation_mid_stream_closes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    async def endless() -> AsyncIterator[bytes]:
        while True:
            yield b"\x00\x00" * 160
            await asyncio.sleep(0.01)

    async with fake_scribe(lambda _m, i: committed("before cancel") if i == 0 else []) as (
        url,
        _log,
    ):
        transcriber = RealtimeTranscriber("k", base_url=url)
        task = asyncio.create_task(
            run_live_session(
                transcriber, TranscriptionService(session_factory, clock=clock), endless()
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    session, turns = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED
    assert [t.text for t in turns] == ["before cancel"]
    assert not transcriber.is_open


async def test_empty_audio_stream_completes_session_with_zero_turns(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    async def empty() -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover - makes this an async generator

    async with fake_scribe(lambda _m, _i: []) as (url, log):
        result = await run_live_session(
            RealtimeTranscriber("k", base_url=url),
            TranscriptionService(session_factory, clock=clock),
            empty(),
            flush_timeout=0.1,
        )

    assert result.turn_count == 0 and result.errors == []
    session, turns = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED and turns == []
    # run_live_session always flushes at end-of-stream, so a commit is still sent
    # even though no real audio chunks went out.
    assert log.messages == [
        {
            "message_type": "input_audio_chunk",
            "audio_base_64": "",
            "commit": True,
            "sample_rate": 16000,
        }
    ]


async def test_audio_source_error_mid_stream_propagates_but_still_closes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    """A failure in the *audio source* (e.g. a mic driver error), not the socket,
    must still flow through the same finally-block cleanup as the other failure
    paths, preserving any turns already committed."""

    class MicFailure(RuntimeError):
        pass

    # Wait for the server's committed segment to actually land before the "mic"
    # dies, instead of racing it with a fixed sleep (deterministic, no flakiness).
    turn_committed = asyncio.Event()

    async def flaky() -> AsyncIterator[bytes]:
        yield b"\x00\x01" * 1600
        await asyncio.wait_for(turn_committed.wait(), timeout=2)
        raise MicFailure("input device disappeared")

    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        return committed("first bit") if idx == 0 else []

    async with fake_scribe(script) as (url, _log):
        with pytest.raises(MicFailure, match="input device disappeared"):
            await run_live_session(
                RealtimeTranscriber("k", base_url=url),
                TranscriptionService(session_factory, clock=clock),
                flaky(),
                on_turn=lambda _segment: turn_committed.set(),
            )

    session, turns = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED and session.ended_at is not None
    assert [t.text for t in turns] == ["first bit"]


async def test_connection_failure_still_closes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    with pytest.raises(OSError):
        await run_live_session(
            RealtimeTranscriber("k", base_url="ws://127.0.0.1:9"),  # nothing listening
            TranscriptionService(session_factory, clock=clock),
            chunks(1),
        )
    session, _ = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED


async def test_second_cancellation_during_cleanup_still_closes_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    # Regression: a second Ctrl+C while the socket close was shielded skipped end_session().
    async def endless() -> AsyncIterator[bytes]:
        while True:
            yield b"\x00\x00" * 160
            await asyncio.sleep(0.01)

    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    async with fake_scribe(lambda _m, _i: []) as (url, _log):
        transcriber = RealtimeTranscriber("k", base_url=url)
        real_stop = transcriber.stop

        async def slow_stop() -> None:
            stop_entered.set()
            await release_stop.wait()
            await real_stop()

        transcriber.stop = slow_stop  # type: ignore[method-assign]
        task = asyncio.create_task(
            run_live_session(
                transcriber, TranscriptionService(session_factory, clock=clock), endless()
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.wait_for(stop_entered.wait(), 2)
        task.cancel()  # second Ctrl+C lands while cleanup is in progress
        with pytest.raises(asyncio.CancelledError):
            await task
        release_stop.set()
        await asyncio.sleep(0.05)  # let the shielded stop finish closing the socket

    session, _ = only_session(session_factory)
    assert session.status == SessionStatus.COMPLETED and session.ended_at is not None


async def test_attached_session_stays_active_when_end_session_on_exit_false(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    with session_factory() as db:
        existing = Session(status=SessionStatus.ACTIVE)
        db.add(existing)
        db.commit()
    service = TranscriptionService(session_factory, clock=clock)
    service.attach_session(existing.id)

    async with fake_scribe(lambda _m, i: committed("hello there") if i == 0 else []) as (
        url,
        _log,
    ):
        result = await run_live_session(
            RealtimeTranscriber("k", base_url=url),
            service,
            chunks(2, gap=0.05),
            flush_timeout=0.1,
            start_new_session=False,
            end_session_on_exit=False,
        )

    assert result.session_id == existing.id and result.turn_count == 1
    session, turns = only_session(session_factory)  # no extra session was created
    assert session.status == SessionStatus.ACTIVE and session.ended_at is None
    assert [(t.source, t.text) for t in turns] == [("voice", "hello there")]


async def test_attached_session_stays_active_after_fatal_error(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    with session_factory() as db:
        existing = Session(status=SessionStatus.ACTIVE)
        db.add(existing)
        db.commit()
    service = TranscriptionService(session_factory, clock=clock)
    service.attach_session(existing.id)

    greeting = [{"message_type": "auth_error", "error": "invalid api key"}]
    async with fake_scribe(lambda _m, _i: [], greeting=greeting) as (url, _log):
        with pytest.raises(FatalTranscriptionError):
            await run_live_session(
                RealtimeTranscriber("bad", base_url=url),
                service,
                chunks(20, gap=0.02),
                start_new_session=False,
                end_session_on_exit=False,
            )
    session, _ = only_session(session_factory)
    assert session.status == SessionStatus.ACTIVE
