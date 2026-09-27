"""WebSocket /sessions/{id}/voice, exercised over a real socket.

The app runs in an in-process uvicorn server on the test's event loop, and the
transcriber talks to ``fake_scribe`` (the real SDK against a scripted local server),
so nothing leaves the machine.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.api.voice import MAX_BUFFERED_CHUNKS, _VoiceStream, error_event
from app.config import Settings
from app.main import create_app
from app.models import Session, SessionStatus, Turn
from app.services.session_service import SessionService
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import RealtimeTranscriber
from tests.fake_scribe_server import CLOSE, Script, ServerLog, committed, fake_scribe

CHUNK = b"\x01\x00" * 1600  # 100 ms of 16 kHz PCM16
TIMEOUT = 5.0


@dataclass
class Harness:
    app: FastAPI
    base_url: str
    sessions: SessionService
    scribe_url: str = "ws://127.0.0.1:9"  # nothing listening unless a test sets it

    def ws_url(self, session_id: str) -> str:
        return f"{self.base_url}/sessions/{session_id}/voice"

    def new_session(self) -> str:
        return self.sessions.create_session().id


@asynccontextmanager
async def serve_app(app: FastAPI) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def harness(session_factory: sessionmaker[Any]) -> AsyncIterator[Harness]:
    holder: list[Harness] = []

    def transcriber_factory() -> RealtimeTranscriber:
        return RealtimeTranscriber("test-fake-key", base_url=holder[0].scribe_url)

    app = create_app(
        settings=Settings(eleven_labs_api_key="test-fake-key", database_url="sqlite://"),
        session_factory=session_factory,
        transcriber_factory=transcriber_factory,
    )
    async with serve_app(app) as base_url:
        h = Harness(app=app, base_url=base_url, sessions=SessionService(session_factory))
        holder.append(h)
        yield h


@asynccontextmanager
async def scribe(h: Harness, script: Script, **kwargs: Any) -> AsyncIterator[ServerLog]:
    def acking(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        # Scribe answers a manual commit even when nothing was buffered; mirror that so
        # the route's end-of-stream flush doesn't sit out its timeout in every test.
        replies = script(msg, idx)
        return replies or (committed("") if msg.get("commit") else [])

    async with fake_scribe(acking, **kwargs) as (url, log):
        h.scribe_url = url
        yield log


async def recv(ws: ClientConnection) -> dict[str, Any]:
    event: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
    return event


async def recv_until(ws: ClientConnection, kind: str) -> list[dict[str, Any]]:
    """Receive events up to and including the first one of ``kind``."""
    events: list[dict[str, Any]] = []
    while True:
        events.append(await recv(ws))
        if events[-1]["type"] == kind:
            return events


async def drain_until_closed(ws: ClientConnection) -> tuple[list[dict[str, Any]], int | None]:
    events: list[dict[str, Any]] = []
    try:
        while True:
            events.append(await recv(ws))
    except ConnectionClosed as exc:
        return events, exc.rcvd.code if exc.rcvd else None


async def close_code_on_connect(url: str) -> tuple[int | None, str]:
    async with connect(url) as ws:
        with pytest.raises(ConnectionClosed) as info:
            await asyncio.wait_for(ws.recv(), TIMEOUT)
    rcvd = info.value.rcvd
    return (rcvd.code, rcvd.reason) if rcvd else (None, "")


async def wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(TIMEOUT):
        while not predicate():
            await asyncio.sleep(0.01)


def stored(h: Harness, session_id: str) -> tuple[Session, list[Turn]]:
    with h.app.state.session_factory() as db:
        session = db.get(Session, session_id)
        turns = list(
            db.scalars(
                select(Turn).where(Turn.session_id == session_id).order_by(Turn.created_at)
            ).all()
        )
    assert session is not None
    return session, turns


def audio_sent(log: ServerLog) -> list[bytes]:
    return [
        base64.b64decode(m["audio_base_64"])
        for m in log.messages
        if m.get("audio_base_64") and not m.get("commit")
    ]


def speaking_script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
    if msg.get("commit"):
        return committed("and a trailing thought", [("and", 1.5, 1.7)])
    if idx == 0:
        return [{"message_type": "partial_transcript", "text": "open the"}]
    if idx == 1:
        return committed("open the browser", [("open", 0.1, 0.3), ("browser", 0.6, 1.0)])
    return []


async def test_round_trip_ready_partial_turn_stop_done(harness: Harness) -> None:
    sid = harness.new_session()
    harness.sessions.add_text_turn(sid, "typed first")
    async with scribe(harness, speaking_script) as log, connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        assert await recv(ws) == {"type": "partial", "text": "open the"}
        await ws.send(CHUNK)
        turn_event = await recv(ws)
        assert turn_event["type"] == "turn"
        turn = turn_event["turn"]
        assert (turn["text"], turn["source"], turn["role"], turn["session_id"]) == (
            "open the browser",
            "voice",
            "user",
            sid,
        )
        assert turn["created_at"].endswith("Z")

        await ws.send(json.dumps({"type": "stop"}))
        rest, code = await drain_until_closed(ws)

    assert [e["type"] for e in rest] == ["turn", "done"]
    assert rest[0]["turn"]["text"] == "and a trailing thought"
    assert rest[1] == {"type": "done", "turn_count": 2}  # voice turns from this stream only
    assert code == 1000
    assert audio_sent(log) == [CHUNK, CHUNK]

    session, turns = stored(harness, sid)
    assert session.status == SessionStatus.ACTIVE and session.ended_at is None
    assert [(t.source, t.text) for t in turns] == [
        ("text", "typed first"),
        ("voice", "open the browser"),
        ("voice", "and a trailing thought"),
    ]
    assert turns[1].id == turn["id"]
    assert not harness.app.state.voice_registry.is_active(sid)


async def test_stop_with_no_audio_finishes_with_zero_turns(harness: Harness) -> None:
    sid = harness.new_session()
    async with scribe(harness, lambda _m, _i: []), connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(json.dumps({"type": "stop"}))
        events, code = await drain_until_closed(ws)
    assert events == [{"type": "done", "turn_count": 0}] and code == 1000


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ("{not json", "bad_json"),
        (json.dumps({"type": "dance"}), "unknown_type"),
        (json.dumps({"no_type": True}), "unknown_type"),
        (json.dumps([1, 2, 3]), "unknown_type"),
        (json.dumps("stop"), "unknown_type"),
    ],
)
async def test_malformed_control_messages_are_non_fatal(
    harness: Harness, frame: str, code: str
) -> None:
    sid = harness.new_session()

    def script(msg: dict[str, Any], _idx: int) -> list[dict[str, Any]]:
        return [] if msg.get("commit") else committed("still works")

    async with scribe(harness, script), connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(frame)
        error = await recv(ws)
        assert error["type"] == "error" and error["code"] == code and error["fatal"] is False
        assert error["message"]
        # The stream is still usable afterwards.
        await ws.send(CHUNK)
        assert (await recv(ws))["turn"]["text"] == "still works"
        await ws.send(json.dumps({"type": "stop"}))
        events, close = await drain_until_closed(ws)
    assert events[-1] == {"type": "done", "turn_count": 1} and close == 1000


async def test_odd_length_binary_frame_is_dropped_with_error(harness: Harness) -> None:
    sid = harness.new_session()
    async with scribe(harness, lambda _m, _i: []) as log, connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(b"\x01\x02\x03")
        error = await recv(ws)
        assert (error["code"], error["fatal"]) == ("bad_audio", False)
        await ws.send(CHUNK)
        await ws.send(json.dumps({"type": "stop"}))
        events, close = await drain_until_closed(ws)
    assert events == [{"type": "done", "turn_count": 0}] and close == 1000
    assert audio_sent(log) == [CHUNK]  # the odd frame never reached ElevenLabs


async def test_audio_after_stop_is_ignored(harness: Harness) -> None:
    sid = harness.new_session()
    async with scribe(harness, lambda _m, _i: []) as log, connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        await ws.send(json.dumps({"type": "stop"}))
        await ws.send(json.dumps({"type": "stop"}))  # duplicate stop is harmless
        await ws.send(b"\x09\x09" * 10)
        events, close = await drain_until_closed(ws)
    assert [e["type"] for e in events] == ["done"] and close == 1000
    assert audio_sent(log) == [CHUNK]


async def test_unknown_session_closes_4404(harness: Harness) -> None:
    code, reason = await close_code_on_connect(harness.ws_url("does-not-exist"))
    assert code == 4404 and "not found" in reason


async def test_closed_session_closes_4409(harness: Harness) -> None:
    sid = harness.new_session()
    harness.sessions.end_session(sid)
    code, _ = await close_code_on_connect(harness.ws_url(sid))
    assert code == 4409


async def test_second_stream_on_same_session_closes_4429_then_slot_frees(
    harness: Harness,
) -> None:
    sid = harness.new_session()
    async with scribe(harness, lambda _m, _i: []):
        async with connect(harness.ws_url(sid)) as first:
            assert await recv(first) == {"type": "ready"}
            code, _ = await close_code_on_connect(harness.ws_url(sid))
            assert code == 4429
            # The rejected attempt must not have released the first stream's slot.
            assert harness.app.state.voice_registry.is_active(sid)
            await first.send(json.dumps({"type": "stop"}))
            await drain_until_closed(first)

        await wait_for(lambda: not harness.app.state.voice_registry.is_active(sid))
        async with connect(harness.ws_url(sid)) as again:
            assert await recv(again) == {"type": "ready"}


async def test_two_sessions_stream_concurrently(harness: Harness) -> None:
    a, b = harness.new_session(), harness.new_session()

    def script(msg: dict[str, Any], _idx: int) -> list[dict[str, Any]]:
        return [] if msg.get("commit") else committed("hello")

    async def talk(sid: str) -> list[dict[str, Any]]:
        async with connect(harness.ws_url(sid)) as ws:
            assert await recv(ws) == {"type": "ready"}
            await ws.send(CHUNK)
            events = await recv_until(ws, "turn")
            await ws.send(json.dumps({"type": "stop"}))
            rest, code = await drain_until_closed(ws)
            assert code == 1000
            return events + rest

    async with scribe(harness, script):
        events_a, events_b = await asyncio.gather(talk(a), talk(b))

    for sid, events in ((a, events_a), (b, events_b)):
        turns = [e["turn"] for e in events if e["type"] == "turn"]
        assert [t["session_id"] for t in turns] == [sid]
        assert events[-1] == {"type": "done", "turn_count": 1}
        _, stored_turns = stored(harness, sid)
        assert [t.text for t in stored_turns] == ["hello"]


@pytest.mark.parametrize("abrupt", [False, True], ids=["close-frame", "tcp-abort"])
async def test_client_disconnect_mid_stream_keeps_session_and_releases_slot(
    harness: Harness, abrupt: bool
) -> None:
    sid = harness.new_session()

    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        return committed("before disconnect") if idx == 0 and not msg.get("commit") else []

    async with scribe(harness, script) as log:
        ws = await connect(harness.ws_url(sid))
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        assert (await recv(ws))["type"] == "turn"
        if abrupt:
            ws.transport.abort()
        else:
            await ws.close()

        await wait_for(lambda: not harness.app.state.voice_registry.is_active(sid))
        # The server still flushed the upstream buffer on the way out.
        assert any(m.get("commit") for m in log.messages)

        session, turns = stored(harness, sid)
        assert session.status == SessionStatus.ACTIVE and session.ended_at is None
        assert [t.text for t in turns] == ["before disconnect"]

        async with connect(harness.ws_url(sid)) as again:
            assert await recv(again) == {"type": "ready"}
            await again.send(json.dumps({"type": "stop"}))
            events, code = await drain_until_closed(again)
        assert events == [{"type": "done", "turn_count": 0}] and code == 1000


async def test_fatal_upstream_error_closes_1011_and_keeps_session(harness: Harness) -> None:
    sid = harness.new_session()
    greeting = [{"message_type": "auth_error", "error": "invalid api key"}]
    async with (
        scribe(harness, lambda _m, _i: [], greeting=greeting),
        connect(harness.ws_url(sid)) as ws,
    ):
        # No audio is sent: the fatal error alone must end the stream.
        events, code = await drain_until_closed(ws)

    errors = [e for e in events if e["type"] == "error"]
    assert errors == [
        {"type": "error", "code": "auth_error", "message": "invalid api key", "fatal": True}
    ]
    assert "done" not in [e["type"] for e in events]
    assert code == 1011
    session, _ = stored(harness, sid)
    assert session.status == SessionStatus.ACTIVE
    assert not harness.app.state.voice_registry.is_active(sid)


async def test_non_fatal_upstream_error_is_forwarded_and_stream_continues(
    harness: Harness,
) -> None:
    sid = harness.new_session()

    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        if msg.get("commit"):
            return []
        return (
            [{"message_type": "rate_limited", "error": "slow down"}]
            if idx == 0
            else committed("made it")
        )

    async with scribe(harness, script), connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        assert await recv(ws) == {
            "type": "error",
            "code": "rate_limited",
            "message": "slow down",
            "fatal": False,
        }
        await ws.send(CHUNK)
        assert (await recv(ws))["turn"]["text"] == "made it"
        await ws.send(json.dumps({"type": "stop"}))
        events, code = await drain_until_closed(ws)
    assert events == [{"type": "done", "turn_count": 1}] and code == 1000


async def test_upstream_closing_unexpectedly_closes_1011(harness: Harness) -> None:
    sid = harness.new_session()
    async with (
        scribe(harness, lambda _m, _i: [CLOSE]),
        connect(harness.ws_url(sid)) as ws,
    ):
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        events, code = await drain_until_closed(ws)
    assert [(e["code"], e["fatal"]) for e in events] == [("upstream_closed", True)]
    assert code == 1011
    assert stored(harness, sid)[0].status == SessionStatus.ACTIVE


async def test_upstream_unreachable_closes_1011(harness: Harness) -> None:
    sid = harness.new_session()  # scribe_url points at a port with nothing listening
    async with connect(harness.ws_url(sid)) as ws:
        events, code = await drain_until_closed(ws)
    assert [(e["type"], e["code"], e["fatal"]) for e in events] == [
        ("error", "upstream_unavailable", True)
    ]
    assert code == 1011
    assert not harness.app.state.voice_registry.is_active(sid)
    assert stored(harness, sid)[0].status == SessionStatus.ACTIVE


async def test_transcriber_config_error_closes_1011(harness: Harness) -> None:
    def broken() -> RealtimeTranscriber:
        raise ValueError("ElevenLabs API key is required")

    harness.app.state.transcriber_factory = broken
    sid = harness.new_session()
    async with connect(harness.ws_url(sid)) as ws:
        events, code = await drain_until_closed(ws)
    assert events == [
        {
            "type": "error",
            "code": "upstream_unavailable",
            "message": "ElevenLabs API key is required",
            "fatal": True,
        }
    ]
    assert code == 1011
    assert not harness.app.state.voice_registry.is_active(sid)


async def test_session_ends_between_accept_and_attach_closes_4409(harness: Harness) -> None:
    """Regression test for the race window `_run` guards with attach_session().

    `voice_stream()` validates the session, then claims the registry slot, then
    `_run()` calls `attach_session()` a second time. If the session ends in that
    window (e.g. a concurrent POST /sessions/{id}/end), attach_session() must be
    the one that catches it and closes 4409 -- and the registry slot must still be
    released.
    """
    sid = harness.new_session()
    original_ensure_active = harness.app.state.session_service.ensure_active

    def ensure_active_then_end_session(session_id: str) -> None:
        original_ensure_active(session_id)
        harness.sessions.end_session(session_id)

    harness.app.state.session_service.ensure_active = ensure_active_then_end_session
    async with scribe(harness, lambda _m, _i: []):
        code, _ = await close_code_on_connect(harness.ws_url(sid))
    assert code == 4409
    assert not harness.app.state.voice_registry.is_active(sid)


async def test_reconnect_after_fatal_error_succeeds(harness: Harness) -> None:
    sid = harness.new_session()
    greeting = [{"message_type": "auth_error", "error": "invalid api key"}]
    async with scribe(harness, lambda _m, _i: [], greeting=greeting):
        async with connect(harness.ws_url(sid)) as ws:
            _, code = await drain_until_closed(ws)
        assert code == 1011

    await wait_for(lambda: not harness.app.state.voice_registry.is_active(sid))

    async with scribe(harness, lambda _m, _i: []), connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(json.dumps({"type": "stop"}))
        events, code = await drain_until_closed(ws)
    assert events == [{"type": "done", "turn_count": 0}] and code == 1000

    session, _ = stored(harness, sid)
    assert session.status == SessionStatus.ACTIVE


async def test_empty_binary_frame_is_ignored_without_error(harness: Harness) -> None:
    sid = harness.new_session()
    async with scribe(harness, lambda _m, _i: []) as log, connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(b"")  # even length (0), must not trip the odd-length check
        await ws.send(CHUNK)
        await ws.send(json.dumps({"type": "stop"}))
        events, close = await drain_until_closed(ws)
    assert events == [{"type": "done", "turn_count": 0}] and close == 1000
    assert audio_sent(log) == [CHUNK]  # the empty frame was dropped, not forwarded


async def test_backpressure_drops_audio_and_emits_non_fatal_error() -> None:
    """Unit-level check of _VoiceStream's audio queue: the real WS round trip can't be
    made to reliably fill a 600-deep queue without timing-dependent sleeps, since
    run_live_session drains it about as fast as the fake upstream accepts sends.
    """
    stream = _VoiceStream(None, "session-under-test")  # type: ignore[arg-type]
    for _ in range(MAX_BUFFERED_CHUNKS):
        stream._handle_audio(CHUNK)
    assert stream.audio.qsize() == MAX_BUFFERED_CHUNKS
    assert stream.outbox.qsize() == 0

    stream._handle_audio(CHUNK)  # queue is full: must be dropped, not blocked on

    assert stream.audio.qsize() == MAX_BUFFERED_CHUNKS  # unchanged, chunk was dropped
    assert stream.outbox.qsize() == 1
    assert stream.outbox.get_nowait() == error_event(
        "backpressure", "Audio buffer full; chunk dropped"
    )

    # A late stop must still be able to signal end-of-audio despite the full queue.
    # end_audio() drops the oldest buffered chunk to make room for the None sentinel.
    stream.stop_requested = True
    stream.end_audio()
    chunks = []
    async for chunk in stream.audio_chunks():
        chunks.append(chunk)
    assert chunks == [CHUNK] * (MAX_BUFFERED_CHUNKS - 1)


async def test_server_shutdown_while_streaming(session_factory: sessionmaker[Any]) -> None:
    """What happens to an in-flight voice stream when uvicorn shuts down.

    Not asserting a specific close code here (see the report): this documents the
    current behaviour so a regression (e.g. the shutdown hanging outright) is caught.
    """
    sessions = SessionService(session_factory)
    sid = sessions.create_session().id

    async with fake_scribe(lambda _m, _i: []) as (scribe_url, _log):

        def transcriber_factory() -> RealtimeTranscriber:
            return RealtimeTranscriber("test-fake-key", base_url=scribe_url)

        app = create_app(
            settings=Settings(eleven_labs_api_key="test-fake-key", database_url="sqlite://"),
            session_factory=session_factory,
            transcriber_factory=transcriber_factory,
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/sessions/{sid}/voice"

        async with connect(url) as ws:
            assert await recv(ws) == {"type": "ready"}

            server.should_exit = True
            async with asyncio.timeout(TIMEOUT):
                await serve_task  # must not hang with a stream still open

            try:
                message = await asyncio.wait_for(ws.recv(), TIMEOUT)
            except ConnectionClosed as exc:
                observed_code = exc.rcvd.code if exc.rcvd else None
            else:
                pytest.fail(f"expected the connection to close, got a message: {message!r}")

        # Even though uvicorn (not our route) issued the close, the route's own
        # cleanup (registry release, no forced session end) must still have run.
        assert not app.state.voice_registry.is_active(sid)
        with session_factory() as db:
            session = db.get(Session, sid)
        assert session is not None and session.status == SessionStatus.ACTIVE

    print(f"[shutdown] client observed close code: {observed_code!r}")


async def test_session_ended_via_rest_mid_stream_stops_voice_and_refuses_turns(
    harness: Harness,
) -> None:
    # Regression (QA): the voice route only checked the session at connect time, so a
    # stream kept writing turns into a session that REST had already completed.
    sid = harness.new_session()

    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        if msg.get("commit"):
            return []
        return committed("first segment") if idx == 0 else committed("after the end")

    async with scribe(harness, script), connect(harness.ws_url(sid)) as ws:
        assert await recv(ws) == {"type": "ready"}
        await ws.send(CHUNK)
        assert (await recv(ws))["turn"]["text"] == "first segment"

        harness.sessions.end_session(sid)  # e.g. POST /sessions/{id}/end from elsewhere

        await ws.send(CHUNK)
        events, code = await drain_until_closed(ws)

    assert events == [
        {
            "type": "error",
            "code": "session_closed",
            "message": "The session was ended; voice input stopped",
            "fatal": True,
        }
    ]
    assert code == 4409
    session, turns = stored(harness, sid)
    assert session.status == SessionStatus.COMPLETED
    assert [t.text for t in turns] == ["first segment"]
    assert not harness.app.state.voice_registry.is_active(sid)


async def test_unexpected_attach_failure_sends_fatal_error_and_closes_1011(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (websocket review): a non-session error from attach_session (e.g. SQLite
    # "database is locked") escaped the route, so the client got no error and no close code.
    def locked(_self: TranscriptionService, _session_id: str) -> None:
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(TranscriptionService, "attach_session", locked)
    sid = harness.new_session()
    async with connect(harness.ws_url(sid)) as ws:
        events, code = await drain_until_closed(ws)

    assert [(e["type"], e["code"], e["fatal"]) for e in events] == [
        ("error", "upstream_unavailable", True)
    ]
    assert "database is locked" in events[0]["message"]
    assert code == 1011
    assert not harness.app.state.voice_registry.is_active(sid)
