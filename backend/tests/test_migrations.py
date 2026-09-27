from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app.db import Base

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
