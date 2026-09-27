from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn, TurnSource


def test_session_crud(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        s = Session()
        db.add(s)
        db.commit()
        sid = s.id

    with session_factory() as db:
        loaded = db.get(Session, sid)
        assert loaded is not None
        assert loaded.status == SessionStatus.ACTIVE
        assert loaded.started_at is not None
        assert loaded.ended_at is None
        loaded.status = SessionStatus.COMPLETED
        db.commit()

    with session_factory() as db:
        loaded = db.get(Session, sid)
        assert loaded is not None and loaded.status == SessionStatus.COMPLETED
        db.delete(loaded)
        db.commit()
        assert db.get(Session, sid) is None


def test_turn_crud_and_relationship(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        s = Session()
        s.turns.append(Turn(text="hello"))
        s.turns.append(Turn(text="world"))
        db.add(s)
        db.commit()
        sid = s.id

    with session_factory() as db:
        turns = db.scalars(select(Turn).where(Turn.session_id == sid)).all()
        assert sorted(t.text for t in turns) == ["hello", "world"]
        assert all(t.role == "user" and t.created_at is not None for t in turns)
        turns[0].text = "edited"
        db.commit()
        db.delete(turns[1])
        db.commit()
        assert [t.text for t in db.scalars(select(Turn)).all()] == ["edited"]


def test_deleting_session_cascades_to_turns(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        s = Session(turns=[Turn(text="a")])
        db.add(s)
        db.commit()
        db.delete(s)
        db.commit()
        assert db.scalars(select(Turn)).all() == []


def test_turn_requires_existing_session(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        db.add(Turn(session_id="does-not-exist", text="orphan"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_turn_text_is_required(session_factory: sessionmaker[Any]) -> None:
    with session_factory() as db:
        s = Session()
        db.add(s)
        db.commit()
        db.add(Turn(session_id=s.id))
        with pytest.raises(IntegrityError):
            db.commit()


def test_turn_source_defaults_to_voice_and_accepts_text(
    session_factory: sessionmaker[Any],
) -> None:
    with session_factory() as db:
        s = Session(turns=[Turn(text="spoken"), Turn(text="typed", source=TurnSource.TEXT)])
        db.add(s)
        db.commit()
    with session_factory() as db:
        by_text = {t.text: t.source for t in db.scalars(select(Turn)).all()}
    assert by_text == {"spoken": "voice", "typed": "text"}
