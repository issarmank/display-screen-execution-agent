"""Live ElevenLabs test. Run explicitly: pytest -m integration (spends a few seconds of credit)."""

import os
import shutil
import subprocess
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from sqlalchemy.orm import sessionmaker

from app.config import REPO_ROOT
from app.services.live_transcription import run_live_session
from app.services.transcription_service import TranscriptionService
from app.speech.elevenlabs_client import RealtimeTranscriber

pytestmark = pytest.mark.integration

PHRASE = "Please open the calendar and create a meeting for tomorrow."


def synthesize_pcm(tmp_path: Path) -> bytes:
    """macOS `say` -> 16 kHz mono PCM16 WAV, then strip the header."""
    if not shutil.which("say"):
        pytest.skip("needs macOS `say`")
    wav = tmp_path / "phrase.wav"
    subprocess.run(
        ["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", PHRASE],
        check=True,
    )
    with wave.open(str(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        return bytes(w.readframes(w.getnframes()))


async def realtime_chunks(pcm: bytes, chunk_ms: int = 100) -> AsyncIterator[bytes]:
    import asyncio

    step = 16000 * 2 * chunk_ms // 1000
    for i in range(0, len(pcm), step):
        yield pcm[i : i + step]
        await asyncio.sleep(chunk_ms / 1000)
    silence = b"\x00" * step
    for _ in range(15):  # 1.5s of silence lets VAD commit
        yield silence
        await asyncio.sleep(chunk_ms / 1000)


async def test_live_scribe_transcribes_and_persists(
    tmp_path: Path, session_factory: sessionmaker[Any]
) -> None:
    key = dotenv_values(REPO_ROOT / ".env").get("ELEVEN_LABS_API_KEY") or os.environ.get(
        "ELEVEN_LABS_API_KEY_LIVE"
    )
    if not key:
        pytest.skip("no real ELEVEN_LABS_API_KEY")
    partials: list[str] = []
    result = await run_live_session(
        RealtimeTranscriber(key, language_code="en"),
        TranscriptionService(session_factory),
        realtime_chunks(synthesize_pcm(tmp_path)),
        on_partial=partials.append,
    )
    assert result.errors == [], result.errors
    assert result.turn_count >= 1
    with session_factory() as db:
        from sqlalchemy import select

        from app.models import Turn

        text = " ".join(t.text for t in db.scalars(select(Turn)).all()).lower()
    print("\npartials:", len(partials), "| transcript:", text)
    assert "calendar" in text and "meeting" in text
