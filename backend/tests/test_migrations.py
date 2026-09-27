from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.db import Base, make_engine

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_creates_tables_matching_models(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "app.db"  # parent dir created by env.py
    command.upgrade(alembic_config(db), "head")

    engine = create_engine(f"sqlite:///{db}")
    insp = inspect(engine)
    assert {"sessions", "turns", "alembic_version"} <= set(insp.get_table_names())
    fks = insp.get_foreign_keys("turns")
    assert fks[0]["referred_table"] == "sessions"

    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"migration drifted from models: {diff}"
    engine.dispose()


def test_downgrade_removes_tables(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    cfg = alembic_config(db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db}")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


def test_0002_backfills_existing_turns_as_voice_and_downgrades(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    cfg = alembic_config(db)
    command.upgrade(cfg, "0001")
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, started_at, status) "
                "VALUES ('s1', '2026-01-01 00:00:00', 'active')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO turns (id, session_id, role, text, created_at) "
                "VALUES ('t1', 's1', 'user', 'old turn', '2026-01-01 00:00:01')"
            )
        )

    command.upgrade(cfg, "0002")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM turns WHERE id = 't1'")).scalar() == "voice"
    assert {c["name"] for c in inspect(engine).get_columns("turns")} >= {"source"}
    # FK and index survive the SQLite batch table rebuild.
    assert inspect(engine).get_foreign_keys("turns")[0]["referred_table"] == "sessions"
    assert "ix_turns_session_id" in {i["name"] for i in inspect(engine).get_indexes("turns")}

    command.downgrade(cfg, "0001")
    cols = {c["name"] for c in inspect(engine).get_columns("turns")}
    assert "source" not in cols
    with engine.connect() as conn:
        assert conn.execute(text("SELECT text FROM turns")).scalar() == "old turn"
    engine.dispose()


def test_make_engine_keeps_foreign_keys_on_with_custom_pool(tmp_path: Path) -> None:
    # Regression: env.py swapped engine.pool after creation, which dropped the
    # foreign-keys listener and the dialect init that batch migrations rely on.
    engine = make_engine(f"sqlite:///{tmp_path / 'fk.db'}", poolclass=NullPool)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    assert engine.dialect.server_version_info is not None
    engine.dispose()
