"""FastAPI entrypoint: ``uvicorn app.main:app`` (from backend/)."""

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.api import sessions
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.services.session_service import SessionService
from app.speech.elevenlabs_client import RealtimeTranscriber

TranscriberFactory = Callable[[], RealtimeTranscriber]


class VoiceRegistry:
    """Tracks which sessions currently have a live voice stream (one per session)."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, session_id: str) -> bool:
        async with self._lock:
            if session_id in self._active:
                return False
            self._active.add(session_id)
            return True

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._active.discard(session_id)

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Any] | None = None,
    transcriber_factory: TranscriberFactory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    if transcriber_factory is None:
        api_key = settings.eleven_labs_api_key

        def transcriber_factory() -> RealtimeTranscriber:
            return RealtimeTranscriber(api_key)

    app = FastAPI(title="Screen Execution Agent")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.session_service = SessionService(session_factory)
    app.state.transcriber_factory = transcriber_factory
    app.state.voice_registry = VoiceRegistry()
    app.include_router(sessions.router)
    return app


app = create_app()
