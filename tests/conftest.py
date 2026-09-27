import os
import re

import pytest

from augury.core.paths import AppPaths, app_paths

# Anything a provider SDK would read as a credential or an endpoint switch.
_PROVIDER_ENV = re.compile(
    r"^(?:.*_API_KEY|GOOGLE_.*|GEMINI_.*|VERTEX.*|LITELLM_.*|AZURE_.*|AWS_.*)$"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Write/overwrite TUI snapshot baselines instead of comparing against them.",
    )


@pytest.fixture(autouse=True)
def no_provider_credentials(request: pytest.FixtureRequest):
    """Offline tests must never see a real key: a developer's shell may export one.
    The whole environment is restored afterwards, so keys a test loads from .env don't leak."""
    if request.node.get_closest_marker("live"):
        yield
        return
    saved = dict(os.environ)
    for name in list(os.environ):
        if _PROVIDER_ENV.match(name):
            del os.environ[name]
    yield
    os.environ.clear()
    os.environ.update(saved)


PUBLIC_TEST_IP = "93.184.215.14"  # a public address: the fake DNS answers it for every name


async def public_dns(host: str) -> list[str]:
    return [PUBLIC_TEST_IP]


@pytest.fixture(autouse=True)
def no_real_dns(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Offline tests never resolve names: the discovery tools' public-address check
    (sources/public_only.py) sees every host name as public unless a test passes its own
    resolver. Literal IPs, localhost and the rest are still judged for real."""
    if not request.node.get_closest_marker("live"):
        monkeypatch.setattr("augury.sources.public_only.system_resolve", public_dns)


@pytest.fixture
def paths(tmp_path, monkeypatch) -> AppPaths:
    monkeypatch.setenv("AUGURY_HOME", str(tmp_path / "home"))
    p = app_paths()
    p.ensure()
    return p


@pytest.fixture
def fast_http(paths: AppPaths) -> AppPaths:
    """Tests that go through the real PoliteClient shouldn't sleep between requests."""
    paths.config_file.write_text("[http]\nmin_interval_s = 0\n")
    return paths
