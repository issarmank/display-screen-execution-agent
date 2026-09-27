from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

MAX_TURN_CHARS = 4000


def _as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo on read; every stored timestamp is UTC, so say so on the wire.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    source: str
    text: str
    started_at: UTCDateTime | None
    ended_at: UTCDateTime | None
    created_at: UTCDateTime


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    started_at: UTCDateTime
    ended_at: UTCDateTime | None


class SessionDetailOut(SessionOut):
    turns: list[TurnOut]


class TextTurnIn(BaseModel):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TURN_CHARS)
    ]
