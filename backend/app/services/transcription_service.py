from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn, TurnSource, utcnow
from app.services.session_service import SessionClosed, SessionNotFound
from app.speech.elevenlabs_client import CommittedSegment


class TranscriptionService:
    """Persists one realtime transcription session and its committed turns."""

    def __init__(
        self,
        session_factory: sessionmaker[Any],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._session_id: str | None = None
        self._audio_anchor: datetime | None = None
        # Set once a segment is refused because the session was ended elsewhere.
        self.session_closed = False

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("No active session; call start_session() first")
        return self._session_id

    def start_session(self) -> Session:
        if self._session_id is not None:
            raise RuntimeError("Session already started")
        with self._session_factory() as db:
            session = Session(started_at=self._clock(), status=SessionStatus.ACTIVE)
            db.add(session)
            db.commit()
        self._session_id = session.id
        self._audio_anchor = session.started_at
        return session

    def attach_session(self, session_id: str) -> None:
        """Record turns into an existing active session instead of creating one."""
        if self._session_id is not None:
            raise RuntimeError("Session already started")
        with self._session_factory() as db:
            session = db.get(Session, session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if session.status != SessionStatus.ACTIVE:
                raise SessionClosed(session_id)
        self._session_id = session_id
        self._audio_anchor = self._clock()

    def mark_audio_started(self, at: datetime | None = None) -> None:
        """Anchor ElevenLabs' stream-relative word offsets to wall-clock time."""
        self._audio_anchor = at or self._clock()

    def record_committed(self, segment: CommittedSegment) -> Turn | None:
        """Persist a committed segment; returns None for blank text or an ended session."""
        text = segment.text.strip()
        if not text:
            return None
        session_id = self.session_id
        with self._session_factory() as db:
            # The conversation may have been ended (REST /end) while audio was streaming.
            session = db.get(Session, session_id)
            if session is None or session.status != SessionStatus.ACTIVE:
                self.session_closed = True
                return None
            turn = Turn(
                session_id=session_id,
                role="user",
                source=TurnSource.VOICE,
                text=text,
                started_at=self._offset(segment.start_s),
                ended_at=self._offset(segment.end_s),
                created_at=self._clock(),
            )
            db.add(turn)
            db.commit()
        return turn

    def turn_count(self) -> int:
        with self._session_factory() as db:
            count = db.scalar(
                select(func.count()).select_from(Turn).where(Turn.session_id == self.session_id)
            )
        return int(count or 0)

    def end_session(self) -> int:
        """Mark the session completed and return its turn count."""
        with self._session_factory() as db:
            session = db.get(Session, self.session_id)
            if session is None:
                raise RuntimeError(f"Session {self.session_id} disappeared")
            session.ended_at = self._clock()
            session.status = SessionStatus.COMPLETED
            db.commit()
            count = db.scalar(
                select(func.count()).select_from(Turn).where(Turn.session_id == session.id)
            )
        return int(count or 0)

    def _offset(self, seconds: float | None) -> datetime | None:
        if seconds is None or self._audio_anchor is None:
            return None
        return self._audio_anchor + timedelta(seconds=seconds)
