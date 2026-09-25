# article-oracle

A terminal-native AI digest reader for research papers and engineering blogs.
Status: **pre-alpha** (v0.1 in development). Supports macOS and Linux.

## Development

```bash
uv sync
uv run pytest            # offline test suite, no API keys needed
uv run ruff check && uv run ruff format --check && uv run pyright
uv run oracle --help
```
