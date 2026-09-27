import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

# Never let tests pick up the real key or dev database.
os.environ["ELEVEN_LABS_API_KEY"] = "test-fake-key"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.db import Base, make_engine, make_session_factory  # noqa: E402


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Any]:
    return make_session_factory(engine)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(seconds=1)
        return current


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
