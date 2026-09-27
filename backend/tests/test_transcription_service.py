from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import CommittedSegment
from tests.conftest import FakeClock


def test_full_session_lifecycle(session_factory: sessionmaker[Any], clock: FakeClock) -> None:
    svc = TranscriptionService(session_factory, clock=clock)
    session = svc.start_session()
    audio_start = clock.now
    svc.mark_audio_started()

    turn = svc.record_committed(CommittedSegment("  open safari  ", start_s=0.5, end_s=1.25))
    assert turn is not None
    svc.record_committed(CommittedSegment("then search for cats"))
    count = svc.end_session()

    assert count == 2
    with session_factory() as db:
        stored = db.get(Session, session.id)
        assert stored is not None
        assert stored.status == SessionStatus.COMPLETED
        assert stored.ended_at is not None and stored.ended_at > stored.started_at
        turns = db.scalars(select(Turn).order_by(Turn.created_at)).all()
        assert [t.text for t in turns] == ["open safari", "then search for cats"]
        assert all(t.session_id == session.id and t.role == "user" for t in turns)
        # SQLite drops tzinfo on read; compare naive wall-clock values.
        first = turns[0]
        assert first.started_at is not None and first.ended_at is not None
        expected_start = (audio_start + timedelta(seconds=0.5)).replace(tzinfo=None)
        assert first.started_at.replace(tzinfo=None) == expected_start
        assert first.ended_at - first.started_at == timedelta(seconds=0.75)
        assert turns[1].started_at is None and turns[1].ended_at is None


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_blank_segments_are_not_persisted(
    session_factory: sessionmaker[Any], clock: FakeClock, text: str
) -> None:
    svc = TranscriptionService(session_factory, clock=clock)
    svc.start_session()
    assert svc.record_committed(CommittedSegment(text)) is None
    assert svc.end_session() == 0


def test_record_before_start_raises(session_factory: sessionmaker[Any]) -> None:
    svc = TranscriptionService(session_factory)
    with pytest.raises(RuntimeError, match="start_session"):
        svc.record_committed(CommittedSegment("hi"))
    with pytest.raises(RuntimeError):
        svc.end_session()


def test_double_start_raises(session_factory: sessionmaker[Any]) -> None:
    svc = TranscriptionService(session_factory)
    svc.start_session()
    with pytest.raises(RuntimeError, match="already"):
        svc.start_session()


def test_turn_count_is_scoped_to_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    other = TranscriptionService(session_factory, clock=clock)
    other.start_session()
    other.record_committed(CommittedSegment("other session"))

    svc = TranscriptionService(session_factory, clock=clock)
    svc.start_session()
    svc.record_committed(CommittedSegment("mine"))
    assert svc.end_session() == 1


def test_attach_session_records_voice_turns_into_existing_session(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    with session_factory() as db:
        existing = Session(status=SessionStatus.ACTIVE)
        db.add(existing)
        db.commit()

    svc = TranscriptionService(session_factory, clock=clock)
    svc.attach_session(existing.id)
    assert svc.session_id == existing.id
    turn = svc.record_committed(CommittedSegment("spoken", start_s=0.0, end_s=0.5))
    assert turn is not None and turn.source == "voice" and turn.started_at is not None
    assert svc.turn_count() == 1
    with session_factory() as db:
        assert db.scalars(select(Session)).all()[0].status == SessionStatus.ACTIVE


def test_attach_unknown_session_raises(session_factory: sessionmaker[Any]) -> None:
    svc = TranscriptionService(session_factory)
    with pytest.raises(LookupError):
        svc.attach_session("nope")
    with pytest.raises(RuntimeError, match="start_session"):
        _ = svc.session_id


def test_attach_closed_session_raises(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        done = Session(status=SessionStatus.COMPLETED)
        db.add(done)
        db.commit()
    svc = TranscriptionService(session_factory)
    with pytest.raises(RuntimeError, match="not active"):
        svc.attach_session(done.id)


def test_attach_after_start_raises(session_factory: sessionmaker[Any]) -> None:
    svc = TranscriptionService(session_factory)
    session = svc.start_session()
    with pytest.raises(RuntimeError, match="already"):
        svc.attach_session(session.id)


def test_record_after_session_ended_elsewhere_is_refused(
    session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    svc = TranscriptionService(session_factory, clock=clock)
    session = svc.start_session()
    assert svc.record_committed(CommittedSegment("before")) is not None
    with session_factory() as db:
        stored = db.get(Session, session.id)
        assert stored is not None
        stored.status = SessionStatus.COMPLETED
        db.commit()

    assert not svc.session_closed
    assert svc.record_committed(CommittedSegment("after")) is None
    assert svc.session_closed
    assert svc.turn_count() == 1
