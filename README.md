# augury

A terminal-native AI digest reader for research papers and engineering blogs.
Status: **pre-alpha** (v0.1 in development). Supports macOS and Linux.

## Getting started

```bash
uv tool install augury   # gets Python 3.14 for you if needed
augury init                      # interests, sources, optional Markdown export folder
augury scout                     # fetch today's papers and posts
augury                           # read them in the terminal
augury sources add https://some.blog/   # add any blog with a feed
augury schedule install          # optional: scout daily in the background
```

## Development

```bash
uv sync
uv run pytest            # offline test suite, no API keys needed
uv run ruff check && uv run ruff format --check && uv run pyright
uv run augury --help
```
