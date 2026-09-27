"""Live mic -> ElevenLabs Scribe v2 Realtime -> SQLite harness.

Usage (from backend/):  python scripts/mic_stream_test.py [--device N] [--language en]
Speak, pause to let VAD commit a segment, Ctrl+C to finish.
"""

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.services.live_transcription import run_live_session  # noqa: E402
from app.services.transcription_service import TranscriptionService  # noqa: E402
from app.speech.audio_source import MicrophoneSource  # noqa: E402
from app.speech.elevenlabs_client import RealtimeTranscriber  # noqa: E402


async def main(device: int | str | None, language: str | None) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    service = TranscriptionService(make_session_factory(engine))
    transcriber = RealtimeTranscriber(settings.eleven_labs_api_key, language_code=language)

    def show_partial(text: str) -> None:
        print(f"\r\033[K… {text}", end="", flush=True)

    def show_turn(segment: object) -> None:
        print(f"\r\033[K✔ {getattr(segment, 'text', '')}", flush=True)

    def show_error(err: dict[str, object]) -> None:
        print(
            f"\r\033[K! {err.get('message_type', 'error')}: {err.get('error', err)}",
            file=sys.stderr,
            flush=True,
        )

    # Treat SIGTERM like Ctrl+C so a killed harness still closes out its session row.
    main_task = asyncio.current_task()
    assert main_task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)

    print(f"DB: {settings.database_url}\nListening… (Ctrl+C to stop)")
    async with MicrophoneSource(device=device) as mic:
        task = asyncio.create_task(
            run_live_session(
                transcriber,
                service,
                mic.chunks(),
                on_partial=show_partial,
                on_turn=show_turn,
                on_error=show_error,
            )
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Ctrl+C: stop the mic so the stream ends, then let the session flush and close.
            await mic.__aexit__(None, None, None)
            result = await task
    print(f"\nSession {result.session_id} completed with {result.turn_count} turn(s).")
    if mic.overflows:
        print(f"(mic input overflowed {mic.overflows} time(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=None, help="sounddevice input device index/name")
    parser.add_argument("--language", default=None, help="ISO language code hint, e.g. en")
    args = parser.parse_args()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main(device, args.language)))
