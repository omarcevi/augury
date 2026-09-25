from pathlib import Path

from augury.core.paths import app_paths


def test_home_override_puts_everything_under_one_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUGURY_HOME", str(tmp_path))
    p = app_paths()
    assert p.config_file == tmp_path / "config" / "config.toml"
    assert p.db_file == tmp_path / "data" / "augury.db"
    assert p.cache_dir == tmp_path / "cache"


def test_default_paths_use_platform_dirs(monkeypatch):
    monkeypatch.delenv("AUGURY_HOME", raising=False)
    p = app_paths()
    assert "augury" in str(p.config_dir)
    assert isinstance(p.data_dir, Path)


def test_ensure_creates_directories(paths):
    assert paths.config_dir.is_dir() and paths.data_dir.is_dir() and paths.cache_dir.is_dir()
