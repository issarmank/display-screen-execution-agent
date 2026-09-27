"""Runs the real harness script as a subprocess with sounddevice/ElevenLabs faked out."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.db import Base, make_engine

BACKEND = Path(__file__).resolve().parents[1]

# Injected via sitecustomize: a silent mic and a Scribe stub, so no hardware or network.
SITECUSTOMIZE = """
import sys, types, threading, time
sd = types.ModuleType("sounddevice")
class RawInputStream:
    def __init__(self, callback, **kw):
        self.cb, self.running = callback, False
    def start(self):
        self.running = True
        def pump():
            while self.running:
                self.cb(b"\\\\x00\\\\x00" * 1600, 1600, None, None)
                time.sleep(0.05)
        threading.Thread(target=pump, daemon=True).start()
    def stop(self):
        self.running = False
    def close(self):
        pass
sd.RawInputStream = RawInputStream
sys.modules["sounddevice"] = sd

import elevenlabs.realtime as rt
class Conn:
    def __init__(self):
        self.h = {}
    def on(self, e, cb):
        self.h.setdefault(e, []).append(cb)
    async def send(self, data):
        for cb in self.h.get("committed_transcript_with_timestamps", []):
            if not getattr(self, "said", False):
                self.said = True
                cb({"text": "hello from the stub", "words": []})
    async def commit(self):
        pass
    async def close(self):
        for cb in self.h.get("close", []):
            cb()
async def connect(self, options):
    return Conn()
rt.ScribeRealtime.connect = connect
"""


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_script_closes_session_on_signal(tmp_path: Path, sig: signal.Signals) -> None:
    # Regression: SIGTERM used to leave the session row stuck in "active".
    db = tmp_path / "app.db"
    Base.metadata.create_all(make_engine(f"sqlite:///{db}"))
    (tmp_path / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{db}",
        "ELEVEN_LABS_API_KEY": "fake",
    }
    proc = subprocess.Popen(
        [sys.executable, "scripts/mic_stream_test.py"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 15
        seen = ""
        while "hello from the stub" not in seen and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            seen += line
        assert "hello from the stub" in seen, seen
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode == 0, seen + out
    assert "completed with 1 turn(s)" in out
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        rows = conn.execute(text("select status, ended_at from sessions")).all()
        turns: list[str] = list(conn.execute(text("select text from turns")).scalars().all())
    engine.dispose()
    assert len(rows) == 1 and rows[0][0] == "completed" and rows[0][1] is not None
    assert turns == ["hello from the stub"]
