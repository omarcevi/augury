import pytest

from augury.core.paths import AppPaths, app_paths


@pytest.fixture
def paths(tmp_path, monkeypatch) -> AppPaths:
    monkeypatch.setenv("AUGURY_HOME", str(tmp_path / "home"))
    p = app_paths()
    p.ensure()
    return p
