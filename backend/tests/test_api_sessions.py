from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.schemas import MAX_TURN_CHARS
from app.config import Settings
from app.main import create_app
from app.models import Session, SessionStatus, Turn


def _no_transcriber() -> Any:
    raise AssertionError("REST tests must not create a transcriber")


@pytest.fixture
def client(session_factory: sessionmaker[Any]) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(eleven_labs_api_key="test-fake-key", database_url="sqlite://"),
        session_factory=session_factory,
        transcriber_factory=_no_transcriber,
    )
    with TestClient(app) as c:
        yield c


def _new_session(client: TestClient) -> str:
    res = client.post("/sessions")
    assert res.status_code == 201
    return str(res.json()["id"])


def _is_utc_iso(value: str) -> bool:
    parsed = datetime.fromisoformat(value)
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_health(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200 and res.json() == {"status": "ok"}


def test_create_session_returns_active_session(
    client: TestClient, session_factory: sessionmaker[Any]
) -> None:
    res = client.post("/sessions")
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "status", "started_at", "ended_at"}
    assert body["status"] == "active" and body["ended_at"] is None
    assert _is_utc_iso(body["started_at"])
    with session_factory() as db:
        assert db.get(Session, body["id"]) is not None


def test_get_session_includes_turns_in_order(client: TestClient) -> None:
    sid = _new_session(client)
    for text in ["one", "two", "three"]:
        assert client.post(f"/sessions/{sid}/turns", json={"text": text}).status_code == 201

    res = client.get(f"/sessions/{sid}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == sid and body["status"] == "active"
    assert [t["text"] for t in body["turns"]] == ["one", "two", "three"]
    assert all(t["source"] == "text" and t["session_id"] == sid for t in body["turns"])
    # Timestamps read back from SQLite are still serialised as UTC.
    assert all(_is_utc_iso(t["created_at"]) for t in body["turns"])
    assert _is_utc_iso(body["started_at"])


def test_get_unknown_session_is_404(client: TestClient) -> None:
    res = client.get("/sessions/does-not-exist")
    assert res.status_code == 404 and "not found" in res.json()["detail"]


def test_add_text_turn_returns_turn_and_persists_it(
    client: TestClient, session_factory: sessionmaker[Any]
) -> None:
    sid = _new_session(client)
    res = client.post(f"/sessions/{sid}/turns", json={"text": "  open safari  "})
    assert res.status_code == 201
    turn = res.json()
    assert set(turn) == {
        "id",
        "session_id",
        "role",
        "source",
        "text",
        "started_at",
        "ended_at",
        "created_at",
    }
    assert (turn["text"], turn["source"], turn["role"], turn["session_id"]) == (
        "open safari",
        "text",
        "user",
        sid,
    )
    with session_factory() as db:
        stored = db.get(Turn, turn["id"])
        assert stored is not None and stored.source == "text"


def test_add_turn_to_unknown_session_is_404(client: TestClient) -> None:
    assert client.post("/sessions/nope/turns", json={"text": "hi"}).status_code == 404


def test_add_turn_to_ended_session_is_409(client: TestClient) -> None:
    sid = _new_session(client)
    assert client.post(f"/sessions/{sid}/end").status_code == 200
    res = client.post(f"/sessions/{sid}/turns", json={"text": "hi"})
    assert res.status_code == 409 and "not active" in res.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": ""},
        {"text": "   \n\t "},
        {"text": "x" * (MAX_TURN_CHARS + 1)},
        {"text": None},
        {"text": 123},
        {"message": "wrong field"},
    ],
    ids=["missing", "empty", "whitespace", "too-long", "null", "number", "wrong-field"],
)
def test_add_turn_validation_errors_are_422(
    client: TestClient, session_factory: sessionmaker[Any], payload: dict[str, Any]
) -> None:
    sid = _new_session(client)
    res = client.post(f"/sessions/{sid}/turns", json=payload)
    assert res.status_code == 422
    with session_factory() as db:
        assert db.scalars(select(Turn)).all() == []


def test_add_turn_invalid_json_body_is_422(client: TestClient) -> None:
    sid = _new_session(client)
    res = client.post(
        f"/sessions/{sid}/turns", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert res.status_code == 422


def test_max_length_text_is_accepted(client: TestClient) -> None:
    sid = _new_session(client)
    res = client.post(f"/sessions/{sid}/turns", json={"text": "x" * MAX_TURN_CHARS})
    assert res.status_code == 201


def test_end_session_completes_it(client: TestClient, session_factory: sessionmaker[Any]) -> None:
    sid = _new_session(client)
    res = client.post(f"/sessions/{sid}/end")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed" and body["ended_at"] is not None
    assert _is_utc_iso(body["ended_at"])
    with session_factory() as db:
        stored = db.get(Session, sid)
        assert stored is not None and stored.status == SessionStatus.COMPLETED


def test_end_session_twice_is_409(client: TestClient) -> None:
    sid = _new_session(client)
    assert client.post(f"/sessions/{sid}/end").status_code == 200
    assert client.post(f"/sessions/{sid}/end").status_code == 409


def test_end_unknown_session_is_404(client: TestClient) -> None:
    assert client.post("/sessions/nope/end").status_code == 404


def test_sessions_are_isolated(client: TestClient) -> None:
    a, b = _new_session(client), _new_session(client)
    client.post(f"/sessions/{a}/turns", json={"text": "for a"})
    assert client.get(f"/sessions/{b}").json()["turns"] == []
