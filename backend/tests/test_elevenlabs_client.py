import asyncio
import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from app.speech.elevenlabs_client import (
    CommittedSegment,
    RealtimeTranscriber,
    segment_from_payload,
)
from tests.fake_scribe_server import CLOSE, committed, fake_scribe


async def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# --- payload parsing -------------------------------------------------------


def test_segment_from_payload_uses_word_bounds_and_ignores_spacing() -> None:
    payload = {
        "text": " hi there ",
        "words": [
            {"text": "hi", "start": 0.2, "end": 0.4, "type": "word"},
            {"text": " ", "start": 0.4, "end": 0.5, "type": "spacing"},
            {"text": "there", "start": 0.5, "end": 0.9, "type": "word"},
            {"text": "(cough)", "start": 1.0, "end": 1.4, "type": "audio_event"},
        ],
    }
    assert segment_from_payload(payload) == CommittedSegment("hi there", 0.2, 0.9)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "no words"},
        {"text": "null words", "words": None},
        {"text": "bad words", "words": ["junk", {"text": "x", "type": "word"}]},
    ],
)
def test_segment_from_payload_without_usable_timestamps(payload: dict[str, Any]) -> None:
    seg = segment_from_payload(payload)
    assert seg.start_s is None and seg.end_s is None
    assert seg.text == payload["text"]


def test_segment_from_payload_missing_text() -> None:
    assert segment_from_payload({}).text == ""
    assert segment_from_payload({"text": None}).text == ""


def test_requires_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        RealtimeTranscriber("")


# --- against a fake Scribe server (real SDK, local socket) -----------------


async def test_handshake_sends_auth_and_expected_query() -> None:
    async with fake_scribe(lambda _m, _i: []) as (url, log):
        t = RealtimeTranscriber("sk-fake", base_url=url, language_code="en")
        await t.start()
        await asyncio.wait_for(t.session_started.wait(), 2)
        await t.stop()

    assert log.headers.get("xi-api-key") == "sk-fake"
    parsed = urlparse(log.path)
    assert parsed.path == "/v1/speech-to-text/realtime"
    q = parse_qs(parsed.query)
    assert q["model_id"] == ["scribe_v2_realtime"]
    assert q["audio_format"] == ["pcm_16000"]
    assert q["commit_strategy"] == ["vad"]
    assert q["include_timestamps"] == ["true"]
    assert q["language_code"] == ["en"]


async def test_audio_round_trip_partial_and_committed() -> None:
    def script(msg: dict[str, Any], idx: int) -> list[dict[str, Any]]:
        if idx == 0:
            return [{"message_type": "partial_transcript", "text": "hel"}]
        if idx == 1:
            return committed("hello world", [("hello", 0.1, 0.4), ("world", 0.5, 0.9)])
        return []

    partials: list[str] = []
    segments: list[CommittedSegment] = []
    pcm = b"\x01\x00\x02\x00" * 800

    async with fake_scribe(script) as (url, log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_partial(partials.append)
        t.on_committed(segments.append)
        await t.start()
        await t.send_chunk(pcm)
        await t.send_chunk(pcm)
        await wait_until(lambda: segments)
        await t.stop()

    assert partials == ["hel"]
    # Both committed messages arrive, but the segment is delivered exactly once.
    assert segments == [CommittedSegment("hello world", 0.1, 0.9)]
    sent = log.messages[0]
    assert sent["message_type"] == "input_audio_chunk"
    assert sent["sample_rate"] == 16000
    assert sent["commit"] is False
    assert base64.b64decode(sent["audio_base_64"]) == pcm


async def test_without_timestamps_uses_plain_committed_event() -> None:
    segments: list[CommittedSegment] = []
    async with fake_scribe(lambda _m, _i: committed("plain")) as (url, log):
        t = RealtimeTranscriber("k", base_url=url, include_timestamps=False)
        t.on_committed(segments.append)
        await t.start()
        await t.send_chunk(b"\x00\x00")
        await wait_until(lambda: segments)
        await t.stop()
    assert segments == [CommittedSegment("plain")]
    assert parse_qs(urlparse(log.path).query)["include_timestamps"] == ["false"]


async def test_empty_committed_segment_is_not_delivered() -> None:
    segments: list[CommittedSegment] = []
    async with fake_scribe(lambda _m, _i: committed("   ")) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_committed(segments.append)
        await t.start()
        await t.send_chunk(b"\x00\x00")
        # flush-style wait: the commit is observed even though nothing is delivered
        await asyncio.sleep(0.2)
        await t.stop()
    assert segments == []


async def test_flush_sends_commit_and_waits_for_segment() -> None:
    def script(msg: dict[str, Any], _i: int) -> list[dict[str, Any]]:
        return committed("tail end") if msg.get("commit") else []

    segments: list[CommittedSegment] = []
    async with fake_scribe(script) as (url, log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_committed(segments.append)
        await t.start()
        await t.send_chunk(b"\x00\x00")
        assert await t.flush(timeout=2) is True
        await t.stop()
    assert segments == [CommittedSegment("tail end")]
    assert log.messages[-1]["commit"] is True


async def test_flush_times_out_when_server_silent() -> None:
    async with fake_scribe(lambda _m, _i: []) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        await t.start()
        assert await t.flush(timeout=0.1) is False
        await t.stop()
    assert await t.flush() is False  # after stop: no-op


async def test_flush_treats_commit_throttled_as_nothing_to_flush() -> None:
    # Regression: stopping right after a VAD commit made Scribe reply commit_throttled,
    # which surfaced as a client-facing error and stalled flush() for its full timeout.
    throttled = {
        "message_type": "commit_throttled",
        "error": "Commit request ignored: only 0.00s of uncommitted audio.",
    }

    def script(msg: dict[str, Any], _i: int) -> list[dict[str, Any]]:
        return [throttled] if msg.get("commit") else []

    errors: list[dict[str, Any]] = []
    async with fake_scribe(script) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_error(errors.append)
        await t.start()
        async with asyncio.timeout(1):  # well under the 5 s flush timeout
            assert await t.flush(timeout=5) is True
        await t.stop()
    assert errors == []


async def test_commit_throttled_outside_flush_still_reaches_error_callback() -> None:
    errors: list[dict[str, Any]] = []
    greeting = [{"message_type": "commit_throttled", "error": "too soon"}]
    async with fake_scribe(lambda _m, _i: [], greeting=greeting) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_error(errors.append)
        await t.start()
        await wait_until(lambda: errors)
        await t.stop()
    assert errors[0]["message_type"] == "commit_throttled"


@pytest.mark.parametrize("error_type", ["auth_error", "quota_exceeded", "rate_limited"])
async def test_server_errors_reach_error_callback(error_type: str) -> None:
    errors: list[dict[str, Any]] = []
    greeting = [{"message_type": error_type, "error": "nope"}]
    async with fake_scribe(lambda _m, _i: [], greeting=greeting) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_error(errors.append)
        await t.start()
        await wait_until(lambda: errors)
        await t.stop()
    assert errors[0]["message_type"] == error_type


async def test_malformed_server_message_reports_error_and_keeps_going() -> None:
    errors: list[dict[str, Any]] = []
    partials: list[str] = []
    greeting: list[dict[str, Any] | str] = [
        "{not json",
        json.dumps({"message_type": "totally_unknown"}),
        {"message_type": "partial_transcript", "text": "still alive"},
    ]
    async with fake_scribe(lambda _m, _i: [], greeting=greeting) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        t.on_error(errors.append)
        t.on_partial(partials.append)
        await t.start()
        await wait_until(lambda: partials)
        await t.stop()
    assert len(errors) == 1 and "parse" in errors[0]["error"]
    assert partials == ["still alive"]


async def test_server_close_sets_closed() -> None:
    async with fake_scribe(lambda _m, _i: [CLOSE]) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        await t.start()
        await t.send_chunk(b"\x00\x00")
        await asyncio.wait_for(t.closed.wait(), 2)
        assert not t.is_open
        await t.stop()


async def test_lifecycle_guards() -> None:
    t = RealtimeTranscriber("k", base_url="ws://127.0.0.1:9")
    with pytest.raises(RuntimeError, match="not started"):
        await t.send_chunk(b"\x00")
    await t.stop()  # stopping an unstarted transcriber is a no-op

    async with fake_scribe(lambda _m, _i: []) as (url, log):
        t = RealtimeTranscriber("k", base_url=url)
        await t.start()
        with pytest.raises(RuntimeError, match="already"):
            await t.start()
        await t.send_chunk(b"")  # empty chunks are dropped, not sent
        await t.stop()
        await t.stop()  # idempotent
    assert log.messages == []


class _ClosingNoisily:
    """Mimics the SDK connection reporting its own close handshake as an error."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, cb: Any) -> None:
        self.handlers.setdefault(event, []).append(cb)

    def emit(self, event: str, *args: Any) -> None:
        for cb in self.handlers.get(event, []):
            cb(*args)

    async def close(self) -> None:
        self.emit("error", {"error": "sent 1000 (OK) User ended conversation; no close frame"})
        self.emit("close")


class _FakeScribe:
    def __init__(self, conn: _ClosingNoisily) -> None:
        self.conn = conn

    async def connect(self, _options: Any) -> _ClosingNoisily:
        return self.conn


async def test_restarting_after_stop_reports_open_connection_correctly() -> None:
    """Regression: start() used to keep the `closed` Event set by a previous stop(),
    so a restarted transcriber reported a live connection as closed and flush() no-oped.
    """
    async with fake_scribe(lambda _m, _i: []) as (url, _log):
        t = RealtimeTranscriber("k", base_url=url)
        await t.start()
        await asyncio.wait_for(t.session_started.wait(), 2)
        await t.stop()
        assert not t.is_open

        await t.start()
        try:
            assert t.is_open, "reconnected transcriber should report open"
            assert await t.flush(timeout=0.2) is False  # server sends nothing, but shouldn't
            #                                              short-circuit as "not open"
        finally:
            await t.stop()


async def test_errors_emitted_during_our_own_close_are_suppressed() -> None:
    # Regression: a clean stop used to surface a bogus "no close frame received" error.
    conn = _ClosingNoisily()
    errors: list[dict[str, Any]] = []
    t = RealtimeTranscriber("", scribe=_FakeScribe(conn))  # type: ignore[arg-type]
    t.on_error(errors.append)
    await t.start()
    conn.emit("error", {"message_type": "rate_limited"})  # before stop: still reported
    await t.stop()
    assert errors == [{"message_type": "rate_limited"}]
    assert t.closed.is_set()
