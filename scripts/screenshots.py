"""Regenerate the SVG screenshots in docs/images/ for the README and the user guide.

Run: uv run python scripts/screenshots.py

Offline, keyless and repeatable. Everything happens in a throwaway home (a
tempfile.TemporaryDirectory), so your own augury data is never read or written. The home is
filled the way a morning scout would fill it, through the project's own code:

- items: the captured Hugging Face API responses in tests/fixtures/hf/, fetched through the real
  adapters (served from disk) and stored with store_items; the blog post in tests/fixtures/pages/
  is enriched and extracted by the real extractor;
- AI results: triage rows written below by hand (each why-read line describes the item from its
  title and abstract only), the day's digest ranked by build_digest in AI mode, and one cached
  TL;DR for the blog post the reader shot opens;
- a few likes, saves and opened items, and a last visit from yesterday evening.

The app runs with a fixed clock, a resolver whose model refuses every call (so "fast: gemini ✓"
in the health bar is what role_statuses reports for a configured key, and nothing is ever sent),
and an HTTP client that only serves the fixtures.
"""

import asyncio
import html
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from textual.pilot import Pilot

from augury.agents.enrich import enrich_new_articles
from augury.agents.normalize import store_items
from augury.agents.rank import build_digest
from augury.agents.scout import ScoutReport, SourceStats
from augury.agents.triage import TriageStats
from augury.core.clock import local_day
from augury.core.config import Config, load_config, load_interests
from augury.core.config_edit import set_value
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.state_repo import StateRepo
from augury.core.db.summaries_repo import SummariesRepo, Summary
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import HIDING_FLAGS, TriageResult
from augury.core.paths import HOME_ENV, AppPaths
from augury.init_wizard import InitAnswers
from augury.init_wizard import apply as write_init_answers
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import NATIVE_PROVIDERS, ResolvedModel, Resolver, Role, model_spec
from augury.sources.http import HttpError, Response
from augury.sources.registry import ADAPTERS
from augury.tui import app as app_module
from augury.tui.app import AuguryApp
from augury.tui.ui_state import UiState
from augury.tui.ui_state import save as save_ui_state
from augury.tui.widgets.config_view import ConfigReport, ConfigView, build_config_report
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.reader_pane import ReaderPane

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "docs" / "images"
SIZE = (160, 45)

# A fixed clock, so ages ("18h"), "Scout: 07:00 (2h ago)" and the digest's recency terms never
# change. The fixtures were captured on 2026-09-25.
LAST_VISIT = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)
SCOUT_AT = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)

HF = "https://huggingface.co"
BLOG_POST = f"{HF}/blog/LiquidAI/lfm2-5-vl-dspark"  # tests/fixtures/pages/hf_blog_post.html
PAGES = {  # URL -> captured response; every other URL is a 404, so nothing reaches the network
    f"{HF}/api/daily_papers?sort=trending&limit=50": "hf/daily_papers.json",
    f"{HF}/api/blog": "hf/blog.json",
    f"{HF}/api/blog/community?sort=trending": "hf/community.json",
    BLOG_POST: "pages/hf_blog_post.html",
}

INTERESTS = InitAnswers(
    about="ML engineer building multimodal apps and agents",
    topics=[
        "world models",
        "vision-language models",
        "LLM agents",
        "efficient inference",
        "evaluation",
    ],
    avoid=["crypto", "funding rounds", "product promos"],
)


class Judgement(NamedTuple):
    relevance: int
    why_read: str
    tags: tuple[str, ...]
    flags: tuple[str, ...] = ()


# What triage would say about each fixture item, for the interests above. Every line sticks to
# what the item's title and abstract (or, for the blog post, its lead paragraph) say.
TRIAGE: dict[str, Judgement] = {
    f"{HF}/papers/2609.24984": Judgement(
        9,
        "Video world model with a camera-queryable implicit 3D memory, for consistent "
        "long-horizon exploration across viewpoints.",
        ("world models", "video generation", "3d"),
    ),
    f"{HF}/papers/2609.24981": Judgement(
        8,
        "Turns a geometry model's features into a latent space for generation, decodable to "
        "appearance, depth, cameras and point maps.",
        ("world models", "3d", "generative models"),
    ),
    f"{HF}/papers/2609.25001": Judgement(
        6,
        "Gameplay data and benchmark suite with multi-horizon instructions, aligned videos and "
        "player actions for evaluating game agents.",
        ("benchmarks", "agents", "games"),
    ),
    f"{HF}/papers/2503.11576": Judgement(
        7,
        "A 256M-parameter VLM that converts whole document pages end to end into DocTags "
        "markup, tables, code and equations included.",
        ("vision-language", "document ai", "small models"),
    ),
    f"{HF}/papers/2412.20138": Judgement(
        5,
        "Multi-agent LLM trading framework with analyst, bull and bear researcher, trader and "
        "risk-management roles.",
        ("agents", "finance"),
    ),
    BLOG_POST: Judgement(
        9,
        "A DSpark draft model adds speculative decoding to LFM2.5-VL-3B: faster generation, "
        "the same outputs, a small memory cost.",
        ("vision-language", "speculative decoding", "inference"),
    ),
    f"{HF}/blog/transformers-llama-cpp-quants": Judgement(
        8,
        "Transformers can now run llama.cpp quantized models.",
        ("quantization", "inference", "transformers"),
    ),
    f"{HF}/blog/evaleval-aisi": Judgement(
        7,
        "UK AISI and EvalEval on making benchmark results reproducible: context for trusting "
        "published scores.",
        ("evaluation", "benchmarks", "reproducibility"),
    ),
    f"{HF}/blog/nvidia/how-to-use-nvidia-warp-and-mjwarp": Judgement(
        4,
        "How-to for NVIDIA Warp and MjWarp to accelerate robotics simulation and learning "
        "workflows.",
        ("robotics", "simulation"),
    ),
    f"{HF}/blog/omlx": Judgement(
        4,
        "Team news: oMLX's creator and maintainer, Jun Kim, joins Hugging Face to support the "
        "MLX community.",
        ("mlx", "community"),
    ),
    f"{HF}/blog/nvidia/nemotron-diarization": Judgement(
        5,
        "Building real-time multi-speaker AI that knows who spoke when, with NVIDIA Nemotron 3 "
        "Diarization.",
        ("speech", "diarization"),
    ),
    f"{HF}/blog/FINAL-Bench/ztc": Judgement(
        5,
        "Claims a model can flag its own wrong answers in 0.06 seconds with zero extra tokens; "
        "check the method first.",
        ("llms", "evaluation"),
    ),
    f"{HF}/blog/mayafree/jve-ecosystems": Judgement(
        3,
        "Compares 13 answer verifiers from the JEV ecosystem on one test set; little else to go "
        "on.",
        ("verifiers",),
        ("thin",),
    ),
    f"{HF}/blog/sora-2/how-to-use-the-jev-ai-model-a-step-by-step-develop": Judgement(
        2,
        "Step-by-step developer guide to the Jev AI model; reads as product promotion.",
        ("tutorial",),
        ("promo",),
    ),
    f"{HF}/blog/sora-2/what-is-jev-ai-a-practical-guide-to-system-one-and": Judgement(
        2,
        "Intro to Jev AI's System One and executable decisions; reads as product promotion.",
        ("tutorial",),
        ("promo",),
    ),
}

# The cached TL;DR for BLOG_POST, from the post's own text (tests/fixtures/pages/hf_blog_post.html).
TLDR = [
    "An experimental DSpark draft model adds speculative decoding to Liquid AI's LFM2.5-VL-3B "
    "without changing output quality.",
    "Decoding runs up to 3.13x faster on device and 2.66x on an H100; end-to-end gains reach "
    "2.62x and 2.27x.",
    "The 280M-parameter drafter adds 8.9% to the 3B model and has day-one support in "
    "llama.cpp, MLX-VLM and SGLang.",
]
TAKEAWAYS = [
    "The drafter conditions on the target's hidden states, so images and text use the same "
    "algorithm as the text drafters.",
    "Only decoding speeds up, not vision encoding or prefill, so end-to-end gains are smaller "
    "where prefill dominates.",
    "Output is exact: the target verifies every drafted token.",
]


class FixtureHttp:
    """An HttpClient that serves the captured pages in PAGES and 404s everything else."""

    on_wait: Callable[[str, float], None] | None = None

    def __init__(self) -> None:
        self.pages = {url: (FIXTURES / name).read_bytes() for url, name in PAGES.items()}

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        respect_robots: bool = True,
    ) -> Response:
        if url not in self.pages:
            raise HttpError(url, "HTTP 404", status=404)
        return Response(url, 200, httpx.Headers(), self.pages[url])

    async def sitemaps(self, url: str) -> list[str]:
        return []


class NoCallsLlm(BaseLlm):
    """Stands in for the configured model: the screenshots must never call one."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        raise AssertionError("scripts/screenshots.py must never call a model")
        yield LlmResponse()  # unreachable; it makes this an async generator, as BaseLlm's is


def offline_resolver(config: Config) -> Resolver:
    """Every role resolves to config.toml's model spec, as with a key set, but nothing is sent."""

    def resolve(role: Role, agent: str | None = None) -> ResolvedModel:
        spec = model_spec(config, role, agent)
        provider, _, name = spec.partition("/")
        return ResolvedModel(spec, provider, provider in NATIVE_PROVIDERS, NoCallsLlm(model=name))

    return resolve


def paths_for(home: Path) -> AppPaths:
    return AppPaths(home / "config", home / "data", home / "cache")  # as AUGURY_HOME lays it out


async def seed(paths: AppPaths) -> None:
    """Fill a fresh home as a 07:00 scout and a little reading would have."""
    conn = open_db(paths, now=SCOUT_AT)
    try:
        # What `augury init` writes (interests.yaml and config.toml; no key, so no .env), plus
        # one setting changed on the config page, so its Source column shows both kinds.
        write_init_answers(paths, INTERESTS, conn=conn, overwrite=True)
        set_value(paths, "budget", "daily_usd", 0.5)
        config = load_config(paths)
        # Yesterday evening's visit: today's items are "new since last visit".
        save_ui_state(
            paths,
            UiState(
                current_visit_started_at=LAST_VISIT,
                current_visit_ended_at=LAST_VISIT + timedelta(minutes=35),
            ),
        )

        runs, sources = RunsRepo(conn), SourcesRepo(conn)
        run_id = runs.start("scout", now=SCOUT_AT)
        http = FixtureHttp()
        stats: dict[str, SourceStats] = {}
        for record in sources.list_all(enabled_only=True):
            source = record.source
            adapter = ADAPTERS[source.recipe.type]
            result = await adapter.fetch(source, sources.fetch_state(source.id), http)
            stored = store_items(conn, result.items, now=SCOUT_AT)
            sources.record_success(source.id, result.state, now=SCOUT_AT)
            stats[source.id] = SourceStats(
                fetched=stored.seen, new=stored.new, skipped=result.skipped + stored.skipped
            )
        # Only the blog post has a captured page; the other articles 404 and keep no summary.
        enriched = await enrich_new_articles(
            conn, http, limit=config.scout.enrich_max_per_run, now=SCOUT_AT
        )

        ids = {str(r["url"]): str(r["id"]) for r in conn.execute("SELECT id, url FROM items")}
        if mismatch := sorted(set(ids) ^ set(TRIAGE)):
            raise SystemExit(f"the fixtures and TRIAGE disagree about: {mismatch}")
        results = [
            TriageResult(
                item_id=ids[url],
                relevance=j.relevance,
                why_read=j.why_read,
                tags=list(j.tags),
                flags=list(j.flags),
            )
            for url, j in TRIAGE.items()
        ]
        TriageRepo(conn).save_all(
            results,
            run_id=run_id,
            model=model_spec(config, "fast", "triage"),
            prompt_version=load_prompt("triage").version,
        )
        digest = build_digest(
            conn, local_day(SCOUT_AT), now=SCOUT_AT, config=config.ranking, ai=True
        )
        SummariesRepo(conn).save(  # made by the scout's prefetch, as the reader caches it
            Summary(
                ids[BLOG_POST],
                load_prompt("summarize").version,
                model_spec(config, "fast", "summarizer"),
                TLDR,
                TAKEAWAYS,
                SCOUT_AT + timedelta(seconds=52),
            )
        )
        report = ScoutReport(
            run_id=run_id,
            status="ok",
            sources=stats,
            new_items=sum(s.new for s in stats.values()),
            enriched=enriched.enriched,
            enrich_error=enriched.error,
            triage=TriageStats(
                attempted=len(results),
                triaged=len(results),
                hidden=sum(1 for r in results if HIDING_FLAGS & set(r.flags)),
                calls=1,
            ),
            digest=digest,
        )
        runs.finish(run_id, "ok", now=SCOUT_AT + timedelta(seconds=58), stats=report.model_dump())

        # A little reading since: two posts opened in the browser (o), two likes, two saves.
        state, at = StateRepo(conn), SCOUT_AT + timedelta(minutes=70)
        state.mark_opened(ids[f"{HF}/blog/nvidia/how-to-use-nvidia-warp-and-mjwarp"], now=at)
        state.mark_opened(ids[f"{HF}/blog/omlx"], now=at + timedelta(minutes=4))
        state.toggle(ids[f"{HF}/blog/transformers-llama-cpp-quants"], "liked", now=at)
        state.toggle(ids[f"{HF}/papers/2503.11576"], "liked", now=at)
        state.toggle(ids[f"{HF}/papers/2609.24981"], "saved", now=at)
        state.toggle(ids[f"{HF}/blog/evaleval-aisi"], "saved", now=at)
    finally:
        conn.close()


# -- no local paths on screen ----------------------------------------------------------------
# The config page lists the config, data and cache folders, which here live under the temp dir
# (/var/folders/..., /tmp/...). Its report is rewritten before it is drawn, replacing each
# folder with the Linux default location it stands for (~/.config/augury, ~/.local/share/augury,
# ~/.cache/augury). Doing it before rendering, rather than editing the SVG afterwards, keeps the
# layout right: Rich sizes every SVG text run to its original length. check_no_local_paths()
# then fails the run if any local path reaches an SVG anyway.
NEUTRAL_DIRS = {
    "config_dir": "~/.config/augury",
    "data_dir": "~/.local/share/augury",
    "cache_dir": "~/.cache/augury",
}


def neutral_report(*args: Any, **kwargs: Any) -> ConfigReport:
    report = build_config_report(*args, **kwargs)
    paths: AppPaths = args[2]  # build_config_report(conn, config, paths, raw_toml, ...)
    dirs = [(str(getattr(paths, name)), shown) for name, shown in NEUTRAL_DIRS.items()]

    def neutral(value: str) -> str:
        for real, shown in dirs:
            if value == real or value.startswith(real + os.sep):
                return shown + value[len(real) :]
        return value

    return replace(report, paths=[(label, neutral(value)) for label, value in report.paths])


def check_no_local_paths(name: str, svg: str, forbidden: list[str]) -> None:
    shown = html.unescape(svg).replace("\xa0", " ")
    for needle in forbidden:
        if needle and needle in shown:
            raise SystemExit(f"{name} shows a local path ({needle!r}); not written")


# -- the shots --------------------------------------------------------------------------------


async def instant() -> None:
    await asyncio.sleep(0)


def refuse_editor(cmd: list[str]) -> None:
    raise AssertionError(f"scripts/screenshots.py must never launch an editor: {cmd}")


def make_app(paths: AppPaths) -> AuguryApp:
    config = load_config(paths)
    # [scout] auto_after_hours stays at its default (12), so the config page shows the real
    # setting; the seeded 07:00 scout is 2 hours old at NOW, so the app starts none.
    # shoot() checks that no scout ran.
    return AuguryApp(
        conn=open_db(paths, now=NOW),
        config=config,
        paths=paths,
        now=lambda: NOW,
        http_factory=lambda _config: FixtureHttp(),
        reader_debounce=instant,
        editor_runner=refuse_editor,
        resolver=offline_resolver(config),
        interests=load_interests(paths),
    )


async def settle(pilot: Pilot[None], ready: Callable[[], object], what: str) -> None:
    deadline = time.monotonic() + 20
    while not ready():
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting for {what}")
        await pilot.pause()
    await pilot.pause()


async def digest_shot(pilot: Pilot[None], app: AuguryApp, shots: dict[str, str]) -> None:
    table = app.query_one(ItemsTable)
    await settle(pilot, lambda: table.row_count and app.query_one(ReaderPane).preview_text, "list")
    shots["digest"] = app.export_screenshot(simplify=True)  # the cursor on the top-ranked item


async def config_shot(pilot: Pilot[None], app: AuguryApp, shots: dict[str, str]) -> None:
    await pilot.press("3")
    table = app.query_one(ConfigView).table
    await settle(pilot, lambda: table.row_count, "the config page")
    # The cursor on a setting config.toml changed (seed() sets it), as if about to edit it.
    keys = [f"{row.section}.{row.field}" for row in table.settings]
    table.move_cursor(row=keys.index("budget.daily_usd"))
    await settle(pilot, lambda: table.cursor_row == keys.index("budget.daily_usd"), "the cursor")
    shots["config"] = app.export_screenshot(simplify=True)


async def reader_shots(pilot: Pilot[None], app: AuguryApp, shots: dict[str, str]) -> None:
    table, reader = app.query_one(ItemsTable), app.query_one(ReaderPane)
    await settle(pilot, lambda: table.row_count, "the list")
    post = next(row.id for row in table.rows_by_key.values() if row.url == BLOG_POST)
    table.select_key(post)
    await pilot.press("enter")

    def opened() -> bool:
        return (
            app.reading_id == post
            and reader.status_message == ""
            and bool(reader.tldr_text)
            and app.reader_settled
        )

    await settle(pilot, opened, "the article and its TL;DR")
    shots["reader"] = app.export_screenshot(simplify=True)
    await pilot.press("z")
    await settle(pilot, lambda: app.screen.has_class("zen") and app.reader_settled, "zen mode")
    shots["zen"] = app.export_screenshot(simplify=True)


Shot = Callable[[Pilot[None], AuguryApp, dict[str, str]], Awaitable[None]]


async def shoot(template: Path, home: Path, take: Shot) -> dict[str, str]:
    """One app run on a fresh copy of the seeded home, so each shot starts from the same state."""
    shutil.copytree(template, home)
    paths = paths_for(home)
    app = make_app(paths)
    shots: dict[str, str] = {}
    try:
        async with app.run_test(size=SIZE) as pilot:
            await take(pilot, app, shots)
            scouts = app.conn.execute("SELECT count(*) FROM runs WHERE kind = 'scout'").fetchone()
            if app.scouting or scouts[0] != 1:  # only the seeded one
                raise SystemExit("a scout ran during the screenshots")
    finally:
        app.conn.close()
    return shots


def clean_environment(home: Path) -> str:
    """Point everything at the throwaway home and drop provider keys; returns the real HOME."""
    real_home = os.environ.get("HOME", "")
    keys = re.compile(r"^(?:.*_API_KEY|GOOGLE_.*|GEMINI_.*|VERTEX.*|LITELLM_.*|AZURE_.*|AWS_.*)$")
    for name in [n for n in os.environ if keys.match(n)]:
        del os.environ[name]
    os.environ[HOME_ENV] = str(home / "augury")  # anything calling app_paths() lands here too
    os.environ["HOME"] = str(home / "user")  # Path.home(): e.g. the config page's schedule line
    os.environ["TZ"] = "UTC"  # "Scout: 07:00", whatever this machine's zone is
    time.tzset()
    return real_home


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="augury-screenshots-") as tmp:
        root = Path(tmp)
        real_home = clean_environment(root)
        (root / "user").mkdir()
        template = root / "augury"
        await seed(paths_for(template))
        app_module.build_config_report = neutral_report  # see NEUTRAL_DIRS

        shots: dict[str, str] = {}
        shots |= await shoot(template, root / "shot-digest", digest_shot)
        shots |= await shoot(template, root / "shot-config", config_shot)
        shots |= await shoot(template, root / "shot-reader", reader_shots)

        forbidden = [tmp, os.path.realpath(tmp), real_home, "/Users/", "/private/", "/var/folders/"]
        OUT.mkdir(parents=True, exist_ok=True)
        for name, svg in shots.items():
            check_no_local_paths(name, svg, forbidden)
        for name, svg in shots.items():
            path = OUT / f"{name}.svg"
            path.write_text(svg, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
