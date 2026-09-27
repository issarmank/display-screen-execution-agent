from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Session, SessionStatus, Turn, utcnow
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

    def mark_audio_started(self, at: datetime | None = None) -> None:
        """Anchor ElevenLabs' stream-relative word offsets to wall-clock time."""
        self._audio_anchor = at or self._clock()

    def record_committed(self, segment: CommittedSegment) -> Turn | None:
        text = segment.text.strip()
        if not text:
            return None
        with self._session_factory() as db:
            turn = Turn(
                session_id=self.session_id,
                role="user",
                text=text,
                started_at=self._offset(segment.start_s),
                ended_at=self._offset(segment.end_s),
                created_at=self._clock(),
            )
            db.add(turn)
            db.commit()
        return turn

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
