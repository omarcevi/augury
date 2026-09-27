# augury

**A terminal-native AI research digest.** Every day augury gathers new AI papers and posts, ranks them for you, and lets you read the full text without leaving the terminal.

![The augury digest: ranked papers and posts with scores, tags and a preview](docs/images/digest.svg)

> **Status: pre-alpha.** v0.1 is feature-complete (milestones M1–M4) and being tested. There's no release on PyPI yet.
> It runs on macOS and Linux and needs Python 3.14, which `uv` installs for you. Apache-2.0.

## Why

There are too many papers and posts, spread over too many sites. Reading them means a browser full of tabs. augury puts them in one fast, keyboard-driven view: it tells you why each item might be worth your time, remembers what you like, and renders whole papers right in the terminal.

## What it does

- **Collects** Hugging Face trending papers, the Hugging Face blog, HF community posts, and any site you add:
  - by URL: `augury sources add jvns.ca` finds the feed with plain code, no AI needed;
  - **by name**, or for a site with no feed: `augury sources add "google tech blogs"`. A **discovery agent** finds a way to fetch it (RSS, sitemap or HTML listing), **tests every recipe for real**, and shows you candidates with sample items. Nothing is added until you confirm.
  - Broken sources are flagged, and `R` re-discovers them.
- **Reads in the terminal:**
  - full arXiv papers, with math shown as Unicode, from arXiv's HTML version or the PDF;
  - blog posts, cleaned up by per-site extractors;
  - a table of contents, zen mode, reading progress, vim keys, and live full-text search.
- **Ranks what matters to you, with or without AI:**
  - *Without an API key:* augury ranks by **your own ★ likes**. It runs a "more like what I liked" search over your liked items, weighs the sources you like and how recent each item is, and explains every score ("matches your likes: diffusion, RL · source you like · 2h ago").
  - *With a key:* a fast model **triages** each new item. It gives a 0–10 relevance score, a one-line "why read", tags, and promo/thin flags. It also writes a **TL;DR with takeaways** when you open an item.
- **Understands what you've read (RAG):**
  - every item is embedded, and opened articles are split into sections;
  - **hybrid search** (keywords + meaning, fused with RRF): press Enter in `/`;
  - **"Also covered by"** groups a paper with the posts about it;
  - a **Related** panel under each article;
  - **Ask** questions about an article or your whole archive, with citations checked in code, so only passages it actually retrieved can be cited.
- **Stays cheap and safe:**
  - a daily budget, checked *before* each model or embedding call;
  - fetched text passed to the model as data, never as instructions;
  - the discovery agent can only reach public addresses;
  - roughly $0.003 to triage 14 posts with the default model.
- **Is polite to the sites it reads:**
  - it respects robots.txt and each site's Crawl-delay;
  - it makes one request per second per site;
  - it uses an honest User-Agent and conditional GETs.
- **Exports** each day's digest as portable Markdown, for any notes app.
- **Is model-agnostic:** Gemini is the default, through Google ADK. Any other provider works through LiteLLM.

## Quick start

augury isn't on PyPI yet, so install it from a clone with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/omarcevi/augury.git
cd augury
uv tool install .              # add '.[providers]' for non-Gemini models (LiteLLM)

augury init                    # your interests, sources, export folder, AI key (all optional)
augury doctor                  # checks paths, config, database, models and network
augury scout                   # fetch today's papers and posts (and, with a key: triage, rank, embed)
augury                         # read them. Press ? for every key

augury sources add "google tech blogs"   # add a source by name (discovery agent, needs a key)
augury reindex                 # rebuild the search index after changing the embedding model
augury eval retrieval          # measure search quality (recall@k, MRR) on your own archive
```

To run it without installing, use `uv run augury …` inside the clone.

**Optional: turn on AI.** Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) and paste it when `augury init` asks. augury saves it to a private `.env` file (mode 600) and never shows it again. Everything else works without a key.

**Optional: scout every morning.** `augury schedule install --time 07:00` sets up launchd on macOS or a systemd timer on Linux. On other systems it prints a cron line for you to add.

## A quick tour

| | |
|---|---|
| ![Reading a post, with its TL;DR](docs/images/reader.svg) | ![Editing settings in the TUI](docs/images/config.svg) |
| **Reader:** the TL;DR and takeaways sit above the article. `h` collapses them, `z` is zen mode, `c` shows the contents. | **Settings (`3`):** edit any setting in place, or reset it to its default. Changes are saved to `config.toml` with your comments kept. |

The essentials:
- `↑/↓` move, `Enter` read.
- `/` search: type to filter by keyword, then Enter for semantic results.
- `l` like, `b` save, `x` hide.
- `s` sort, `v` show, `#` tags.
- `a` ask about the open item, `A` ask across the archive.
- `r` scout now, `t` theme, `?` help.
- `2` Sources: `+` add by URL or name, `R` re-discover a broken source.
- `3` Settings.

**➡️ The full [user guide](docs/guide.md)** covers every key, how ranking works, AI setup, sources, every setting, and where your data lives.

## How it works

```mermaid
flowchart LR
    S[Sources<br/>HF papers · HF blog · community<br/>RSS · sitemap · HTML listing] --> F[fetch<br/>polite HTTP]
    F --> ST[store<br/>dedupe]
    ST --> E[enrich<br/>opening paragraphs]
    E --> I[ingest<br/>embed + cluster]
    I --> T[triage<br/>fast model, optional]
    T --> R[rank<br/>AI or your likes]
    R --> P[prefetch<br/>top items + TL;DRs]
    P --> X[export<br/>Markdown]
    R --> DB[(SQLite<br/>FTS5 + sqlite-vec)]
    DB --> TUI[Textual TUI<br/>search · Related · Ask]
    D[discovery agent<br/>tested recipes only] -.-> S
```

- **One scout, one workflow.** The steps above are nodes of a single [Google ADK](https://google.github.io/adk-docs/) graph workflow. Each step is isolated, so a failing AI step never loses the items a scout already stored.
- **Discovery and Ask are standalone agents.** They run outside the scout, with hard caps (20 tool calls or 90 s for discovery), and every call counts against the budget. What they propose is checked in code: recipes must pass a real test, citations must point at passages that were actually retrieved.
- **Everything lives in one SQLite file** (WAL mode, FTS5 keyword search, sqlite-vec vectors, numbered migrations). You can open it with any SQLite client. Without an embedder or sqlite-vec, everything still works with keyword search.
- **The TUI never fetches on cursor movement.** The preview shows only cached text. Articles are fetched when you open them, or ahead of time for the top of the digest.

## Roadmap

v0.1 was built in milestones, and all four are done:

- ✅ **M1: the reader.** Sources, polite fetching, full-text reading, search, the TUI.
- ✅ **M2: the ranked digest.** Triage, ranking (AI or likes), TL;DRs, budget, doctor, export, editable settings.
- ✅ **M3: the discovery agent.** Add any site by URL *or by name*. The agent finds a way to fetch it (RSS, sitemap, or an HTML listing) and keeps a recipe only after it passes a real test. Source health and re-discover are part of it.
- ✅ **M4: RAG.** Embeddings, hybrid search, "Also covered by" clusters, novelty in ranking, Related, Ask with checked citations, and a retrieval eval.

Next: live testing and a round of fixes. After that, v0.2 "Studio": drafting posts and newsletters in your own voice from what you've read.

## Development

```bash
uv sync --all-extras
uv run pytest                    # ~1,250 tests, fully offline: no network, no API keys
uv run pytest -m live            # opt-in live model checks (needs a key in the environment)
uv run ruff check && uv run ruff format --check && uv run pyright
uv run augury eval retrieval --fake-embedder   # the retrieval eval's code path, offline and free
uv run python scripts/screenshots.py   # regenerate docs/images/*.svg
```

The TUI has SVG snapshot tests. Run `uv run pytest --snapshot-update` to refresh the baselines after an intended UI change.

For a scratch install that leaves your real data alone, set `AUGURY_HOME=/tmp/augury-dev` before running any command.

## License

[Apache-2.0](LICENSE)
