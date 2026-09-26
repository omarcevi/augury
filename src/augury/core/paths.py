import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "augury"
HOME_ENV = "AUGURY_HOME"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def interests_file(self) -> Path:
        return self.config_dir / "interests.yaml"

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "augury.db"

    @property
    def sessions_db_file(self) -> Path:
        return self.data_dir / "sessions.db"

    @property
    def scout_lock_file(self) -> Path:
        return self.data_dir / "scout.lock"

    @property
    def ui_state_file(self) -> Path:
        """What the TUI remembers between runs (theme, filters, last visit); never config.toml."""
        return self.data_dir / "ui_state.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def probe_cache_file(self) -> Path:
        return self.cache_dir / "probes.json"

    def ensure(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.cache_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


def app_paths() -> AppPaths:
    override = os.environ.get(HOME_ENV)
    if override:
        root = Path(override).expanduser()
        return AppPaths(root / "config", root / "data", root / "cache")
    return AppPaths(
        platformdirs.user_config_path(APP_NAME, appauthor=False),
        platformdirs.user_data_path(APP_NAME, appauthor=False),
        platformdirs.user_cache_path(APP_NAME, appauthor=False),
    )
