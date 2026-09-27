import pytest

from app.config import DEFAULT_DATABASE_URL, REPO_ROOT, Settings


def test_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVEN_LABS_API_KEY", "abc")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.eleven_labs_api_key == "abc"
    assert s.database_url == "sqlite:///x.db"


def test_default_database_is_repo_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ELEVEN_LABS_API_KEY", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_url == DEFAULT_DATABASE_URL
    assert DEFAULT_DATABASE_URL.endswith(str(REPO_ROOT / "data" / "app.db"))
    assert s.eleven_labs_api_key == ""
