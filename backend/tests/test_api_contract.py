"""Pins the JSON the backend sends to the Swift app.

The Swift decoding tests load the same fixture files, so a change to the wire
format fails on both sides until the fixtures are regenerated on purpose:

    UPDATE_CONTRACT_FIXTURES=1 pytest tests/test_api_contract.py
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.api.schemas import SessionDetailOut, SessionOut, TurnOut
from app.api.voice import done_event, error_event, partial_event, ready_event, turn_event
from app.models import Session, SessionStatus, Turn, TurnSource

FIXTURES = (
    Path(__file__).resolve().parents[2] / "frontend" / "Tests" / "ScreenAgentCoreTests" / "Fixtures"
)
SESSION_ID = "8a4c7a2e-1f3b-4c1e-9d0a-5b6f7e8d9c01"
# SQLite hands back naive datetimes; build the samples the same way.
T0 = datetime(2026, 9, 27, 18, 30, 0)
T1 = datetime(2026, 9, 27, 18, 30, 1, 250000)


def _voice_turn() -> Turn:
    return Turn(
        id="3f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0",
        session_id=SESSION_ID,
        role="user",
        source=TurnSource.VOICE,
        text="open the browser",
        started_at=T0,
        ended_at=T1,
        created_at=T1,
    )


def _voice_turn_without_timestamps() -> Turn:
    return Turn(
        id="0a1b2c3d-4e5f-4061-8273-94a5b6c7d8e9",
        session_id=SESSION_ID,
        role="user",
        source=TurnSource.VOICE,
        text="and a trailing thought",
        started_at=None,
        ended_at=None,
        created_at=datetime(2026, 9, 27, 18, 30, 2, tzinfo=UTC),
    )


def _text_turn() -> Turn:
    return Turn(
        id="b7c8d9e0-f1a2-4b3c-8d4e-5f6a7b8c9d0e",
        session_id=SESSION_ID,
        role="user",
        source=TurnSource.TEXT,
        text="open safari",
        started_at=T0,
        ended_at=T0,
        created_at=T0,
    )


def voice_events() -> dict[str, Any]:
    return {
        "events": [
            ready_event(),
            partial_event("open the"),
            turn_event(_voice_turn()),
            turn_event(_voice_turn_without_timestamps()),
            error_event("bad_json", "Control message is not valid JSON"),
            error_event("auth_error", "invalid api key", fatal=True),
            done_event(2),
        ]
    }


def rest_samples() -> dict[str, Any]:
    active = Session(id=SESSION_ID, status=SessionStatus.ACTIVE, started_at=T0, ended_at=None)
    ended = Session(id=SESSION_ID, status=SessionStatus.COMPLETED, started_at=T0, ended_at=T1)
    detail = Session(
        id=SESSION_ID,
        status=SessionStatus.ACTIVE,
        started_at=T0,
        ended_at=None,
        turns=[_text_turn(), _voice_turn()],
    )
    return {
        "session": SessionOut.model_validate(active).model_dump(mode="json"),
        "session_ended": SessionOut.model_validate(ended).model_dump(mode="json"),
        "session_detail": SessionDetailOut.model_validate(detail).model_dump(mode="json"),
        "text_turn": TurnOut.model_validate(_text_turn()).model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    ("name", "build"),
    [("voice_events.json", voice_events), ("rest_samples.json", rest_samples)],
)
def test_contract_fixture_matches_server_output(name: str, build: Any) -> None:
    path = FIXTURES / name
    expected = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_CONTRACT_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected)
    assert path.exists(), f"{path} missing; run with UPDATE_CONTRACT_FIXTURES=1"
    assert path.read_text() == expected, (
        f"{name} drifted from the server's wire format; if intentional, regenerate with "
        "UPDATE_CONTRACT_FIXTURES=1 and update the Swift decoders"
    )


def test_every_event_type_is_in_the_fixture() -> None:
    types = {e["type"] for e in voice_events()["events"]}
    assert types == {"ready", "partial", "turn", "error", "done"}
