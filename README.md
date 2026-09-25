# augury

A terminal-native AI digest reader for research papers and engineering blogs.
Status: **pre-alpha** (v0.1 in development). Supports macOS and Linux.

## Getting started

augury isn't on PyPI yet, so install it from a clone:

```bash
git clone https://github.com/omarcevi/augury.git
cd augury
uv tool install .              # gets Python 3.14 for you if needed
augury init                    # interests, sources, optional Markdown export folder
augury doctor                  # check paths, config, database and network
augury scout                   # fetch today's papers and posts
augury                         # read them in the terminal (? shows every key)
augury sources add jvns.ca     # add any blog with a feed (https:// is assumed)
augury schedule install        # optional: scout daily in the background
```

To try it without installing, run the same commands as `uv run augury …` inside the clone.

## Development

```bash
uv sync
uv run pytest            # offline test suite, no API keys needed
uv run ruff check && uv run ruff format --check && uv run pyright
uv run augury --help
```
