import pytest

from augury.core.paths import AppPaths, app_paths


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Write/overwrite TUI snapshot baselines instead of comparing against them.",
    )


@pytest.fixture
def paths(tmp_path, monkeypatch) -> AppPaths:
    monkeypatch.setenv("AUGURY_HOME", str(tmp_path / "home"))
    p = app_paths()
    p.ensure()
    return p
