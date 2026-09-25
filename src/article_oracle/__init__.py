"""Article Oracle: a terminal-native AI digest reader."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("article-oracle")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
