"""Conversation session lifecycle, independent of any one voice stream."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload, sessionmaker

from app.models import Session, SessionStatus, Turn, TurnSource, utcnow


class SessionNotFound(LookupError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session {session_id} not found")
        self.session_id = session_id


class SessionClosed(RuntimeError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session {session_id} is not active")
        self.session_id = session_id


class SessionService:
    def __init__(
        self,
        session_factory: sessionmaker[Any],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def create_session(self) -> Session:
        with self._session_factory() as db:
            session = Session(started_at=self._clock(), status=SessionStatus.ACTIVE)
            db.add(session)
            db.commit()
            db.refresh(session, ["turns"])
        return session

    def get_session(self, session_id: str) -> Session:
        """Load a session with its turns (ordered by creation) eagerly attached."""
        with self._session_factory() as db:
            session: Session | None = db.scalar(
                select(Session).where(Session.id == session_id).options(selectinload(Session.turns))
            )
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def ensure_active(self, session_id: str) -> None:
        with self._session_factory() as db:
            session = db.get(Session, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if session.status != SessionStatus.ACTIVE:
                raise SessionClosed(session_id)

    def add_text_turn(self, session_id: str, text: str) -> Turn:
        text = text.strip()
        if not text:
            raise ValueError("Turn text must not be blank")
        with self._session_factory() as db:
            session = db.get(Session, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if session.status != SessionStatus.ACTIVE:
                raise SessionClosed(session_id)
            now = self._clock()
            turn = Turn(
                session_id=session_id,
                role="user",
                source=TurnSource.TEXT,
                text=text,
                started_at=now,
                ended_at=now,
                created_at=now,
            )
            db.add(turn)
            db.commit()
        return turn

    def end_session(self, session_id: str) -> Session:
        with self._session_factory() as db:
            session: Session | None = db.get(Session, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if session.status != SessionStatus.ACTIVE:
                raise SessionClosed(session_id)
            session.ended_at = self._clock()
            session.status = SessionStatus.COMPLETED
            db.commit()
        return session
