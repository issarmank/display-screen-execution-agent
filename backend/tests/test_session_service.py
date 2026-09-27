from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn, TurnSource
from app.services.session_service import SessionClosed, SessionNotFound, SessionService
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import CommittedSegment
from tests.conftest import FakeClock


@pytest.fixture
def svc(session_factory: sessionmaker[Any], clock: FakeClock) -> SessionService:
    return SessionService(session_factory, clock=clock)


def test_create_session_is_active_with_no_turns(
    svc: SessionService, session_factory: sessionmaker[Any]
) -> None:
    session = svc.create_session()
    assert session.status == SessionStatus.ACTIVE and session.ended_at is None
    assert session.turns == []
    with session_factory() as db:
        assert db.get(Session, session.id) is not None


def test_add_text_turn_persists_stripped_text_with_text_source(
    svc: SessionService, session_factory: sessionmaker[Any]
) -> None:
    session = svc.create_session()
    turn = svc.add_text_turn(session.id, "  open safari \n")
    assert turn.text == "open safari" and turn.source == TurnSource.TEXT
    with session_factory() as db:
        stored = db.get(Turn, turn.id)
        assert stored is not None
        assert (stored.session_id, stored.role, stored.source, stored.text) == (
            session.id,
            "user",
            "text",
            "open safari",
        )


def test_get_session_returns_turns_in_creation_order_with_mixed_sources(
    svc: SessionService, session_factory: sessionmaker[Any], clock: FakeClock
) -> None:
    session = svc.create_session()
    svc.add_text_turn(session.id, "first typed")
    voice = TranscriptionService(session_factory, clock=clock)
    voice.attach_session(session.id)
    voice.record_committed(CommittedSegment("then spoken"))
    svc.add_text_turn(session.id, "typed again")

    loaded = svc.get_session(session.id)
    assert [(t.source, t.text) for t in loaded.turns] == [
        ("text", "first typed"),
        ("voice", "then spoken"),
        ("text", "typed again"),
    ]


def test_ensure_active_does_not_raise_for_active_session(svc: SessionService) -> None:
    session = svc.create_session()
    svc.ensure_active(session.id)  # no exception


def test_get_unknown_session_raises_not_found(svc: SessionService) -> None:
    with pytest.raises(SessionNotFound):
        svc.get_session("nope")


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text_turn_is_rejected(svc: SessionService, text: str) -> None:
    session = svc.create_session()
    with pytest.raises(ValueError):
        svc.add_text_turn(session.id, text)


def test_add_turn_to_unknown_session_raises_not_found(svc: SessionService) -> None:
    with pytest.raises(SessionNotFound):
        svc.add_text_turn("nope", "hi")


def test_end_session_completes_it_and_blocks_further_turns(
    svc: SessionService, session_factory: sessionmaker[Any]
) -> None:
    session = svc.create_session()
    svc.add_text_turn(session.id, "hi")
    ended = svc.end_session(session.id)
    assert ended.status == SessionStatus.COMPLETED and ended.ended_at is not None

    with pytest.raises(SessionClosed):
        svc.add_text_turn(session.id, "too late")
    with pytest.raises(SessionClosed):
        svc.end_session(session.id)
    with pytest.raises(SessionClosed):
        svc.ensure_active(session.id)
    with session_factory() as db:
        assert len(db.scalars(select(Turn)).all()) == 1


def test_end_unknown_session_raises_not_found(svc: SessionService) -> None:
    with pytest.raises(SessionNotFound):
        svc.end_session("nope")
    with pytest.raises(SessionNotFound):
        svc.ensure_active("nope")
