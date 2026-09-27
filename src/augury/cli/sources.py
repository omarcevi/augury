import asyncio
import sqlite3

import click

from augury.agents.discovery.agent import (
    DiscoveryDeps,
    DiscoveryOutcome,
    classify_input,
    discover,
)
from augury.agents.discovery.confirm import add_candidates
from augury.agents.discovery.guards import Progress
from augury.agents.discovery.models import Candidate, primary_url
from augury.cli.output import safe
from augury.core.clock import utcnow
from augury.core.config import Config, ConfigError, load_config
from augury.core.db.open import open_db
from augury.core.db.sources_repo import BuiltinSourceError, SourcesRepo
from augury.core.models import FetchState, Source
from augury.core.paths import app_paths
from augury.llm.probes import load_probe_results
from augury.llm.resolver import default_resolver
from augury.sources.base import AdapterError
from augury.sources.http import HttpError, PoliteClient
from augury.sources.probe import ProbeResult, build_feed_source, page_url, probe_url
from augury.sources.registry import adapter_for
from augury.sources.rss import FeedInfo, inspect_feed


def _open() -> tuple[Config, sqlite3.Connection]:
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    return config, open_db(paths)


@click.group("sources")
def sources_group() -> None:
    """List, add, test and manage sources."""


@sources_group.command("list")
def list_cmd() -> None:
    """Show every source with its health."""
    _, conn = _open()
    try:
        records = SourcesRepo(conn).list_all()
    finally:
        conn.close()
    click.echo(f"{'ID':<20} {'TYPE':<13} {'ON':<4} {'HEALTH':<9} {'LAST SUCCESS':<17} LAST ERROR")
    for r in records:
        last = (
            r.last_success_at.astimezone().strftime("%Y-%m-%d %H:%M")
            if r.last_success_at
            else "never"
        )
        on = "on" if r.source.enabled else "off"
        click.echo(
            f"{r.source.id:<20} {r.source.recipe.type:<13} {on:<4} {r.health:<9} {last:<17} "
            f"{safe(r.last_error or '')[:60]}"
        )


def add_feed_source(
    conn: sqlite3.Connection, info: FeedInfo, *, name: str | None, added_via: str, yes: bool
) -> Source:
    repo = SourcesRepo(conn)
    source = build_feed_source(repo, info, name=name, added_via=added_via)
    newest = f", newest {info.newest:%Y-%m-%d}" if info.newest else ""
    click.echo(safe(f"{source.name}  ({info.feed_url})\n  {info.entries} entries{newest}"))
    for title in info.sample_titles:
        click.echo(safe(f"  · {title}"))
    if not yes and not click.confirm(f"Add as {source.id!r}?", default=True):
        raise click.Abort()
    repo.add(source, now=utcnow())
    click.echo(f"Added {source.id}. It will be fetched on the next scout.")
    return source


@sources_group.command("add")
@click.argument("target", metavar="URL_OR_NAME", required=False)
@click.option("--rss", "feed_url", metavar="URL", help="Add this RSS/Atom feed directly.")
@click.option("--name", help="Display name (defaults to the feed's title).")
@click.option("--yes", "-y", is_flag=True, help="Don't ask; take the first usable candidate.")
def add(target: str | None, feed_url: str | None, name: str | None, yes: bool) -> None:
    """Add a source from a blog's URL (its feed is found for you), a publication's name
    ("google tech blogs": the discovery agent finds and tests candidates), or --rss URL."""
    if bool(target) == bool(feed_url):
        raise click.UsageError("pass either a URL or name, or --rss URL")
    config, conn = _open()
    try:
        if feed_url:
            _add_rss(conn, config, feed_url, name, yes)
            return
        assert target is not None
        kind, value = classify_input(target)
        if kind == "url":
            try:
                value = page_url(value)
            except ValueError as e:
                raise click.ClickException(safe(e)) from e
            result = _probe(config, value)
            if result is not None and result.candidates:
                info = _choose(result.candidates, yes)
                if existing := SourcesRepo(conn).find_by_feed_url(info.feed_url):
                    raise click.ClickException(safe(f"that feed is already added as {existing!r}"))
                add_feed_source(conn, info, name=name, added_via="url_probe", yes=yes)
                return
            if result is not None:
                for attempt in result.attempts:
                    click.echo(safe(f"  tried {attempt.url}: {attempt.outcome}"))
            click.echo("No feed on that page. Asking the discovery agent…")
        else:
            result = None
        _discover_and_add(conn, config, value, yes=yes, probe=result)
    finally:
        conn.close()


def _add_rss(
    conn: sqlite3.Connection, config: Config, feed_url: str, name: str | None, yes: bool
) -> None:
    try:
        feed_url = page_url(feed_url)
    except ValueError as e:
        raise click.ClickException(safe(e)) from e
    repo = SourcesRepo(conn)
    if existing := repo.find_by_feed_url(feed_url):
        raise click.ClickException(safe(f"that feed is already added as {existing!r}"))

    async def inspect() -> FeedInfo:
        async with PoliteClient(config.http) as http:
            return await inspect_feed(feed_url, http)

    try:
        info = asyncio.run(inspect())
    except (HttpError, AdapterError) as e:
        raise click.ClickException(safe(e)) from e
    if existing := repo.find_by_feed_url(info.feed_url):
        raise click.ClickException(safe(f"that feed is already added as {existing!r}"))
    add_feed_source(conn, info, name=name, added_via="manual", yes=yes)


def _probe(config: Config, url: str) -> ProbeResult | None:
    async def probe() -> ProbeResult:
        async with PoliteClient(config.http) as http:
            return await probe_url(url, http, now=utcnow())

    try:
        return asyncio.run(probe())
    except HttpError as e:
        click.echo(safe(f"  couldn't read {url}: {e}"))
        return None


def show_progress(line: Progress) -> None:
    if line.done:
        mark = "✓" if line.ok else "✗"
        click.echo(safe(f"    {mark} {line.detail}" if line.detail else f"    {mark}"))
    else:
        click.echo(safe(f"  → {line.tool} {line.detail}".rstrip()))


def describe_candidate(i: int, c: Candidate) -> list[str]:
    dup = f"  [duplicate of {c.duplicate_of}]" if c.duplicate_of else ""
    lines = [f"{i}. {c.name}  ({c.recipe.type}: {primary_url(c.recipe)}){dup}"]
    if c.note:
        lines.append(f"   {c.note}  (confidence {c.confidence:.0%})")
    lines += [f"   · {s.title}" for s in c.sample_items]
    return [safe(line) for line in lines]


def _discover_and_add(
    conn: sqlite3.Connection,
    config: Config,
    query: str,
    *,
    yes: bool,
    probe: ProbeResult | None,
) -> None:
    paths = app_paths()

    async def go() -> DiscoveryOutcome:
        async with PoliteClient(config.http) as http:
            deps = DiscoveryDeps(
                conn=conn,
                http=http,
                config=config,
                resolver=default_resolver(config),
                sessions_db=paths.sessions_db_file,
                now=utcnow,
                probes=load_probe_results(paths.probe_cache_file),
            )
            return await discover(
                query, deps, on_progress=show_progress, probe=probe, skip_probe=probe is None
            )

    outcome = asyncio.run(go())
    if outcome.via == "none":  # no agent (no smart model, a failed tool probe, the budget)
        raise click.ClickException(
            safe(f"{outcome.explanation}. If you know the feed URL, use --rss.")
        )
    if outcome.explanation:
        click.echo(safe(f"Note: {outcome.explanation}"))
    usable = [c for c in outcome.candidates if not c.duplicate]
    for i, c in enumerate(outcome.candidates, start=1):
        click.echo("\n".join(describe_candidate(i, c)))
    if not usable:
        raise click.ClickException("no new source to add")
    if yes:
        chosen = usable[:1]
    else:
        default = str(outcome.candidates.index(usable[0]) + 1)
        answer = click.prompt("Add which? (numbers, comma-separated; 0 for none)", default=default)
        chosen = _pick(answer, outcome.candidates)
    added = add_candidates(conn, chosen, run_id=outcome.run_id, now=utcnow())
    for source in added:
        click.echo(safe(f"Added {source.id}. It will be fetched on the next scout."))
    if not added:
        click.echo("Nothing added.")


def _pick(answer: str, candidates: list[Candidate]) -> list[Candidate]:
    chosen: list[Candidate] = []
    for part in answer.replace(" ", "").split(","):
        if not part or part == "0":
            continue
        if not part.isdigit() or not 1 <= int(part) <= len(candidates):
            raise click.ClickException(f"{part!r} is not one of 1-{len(candidates)}")
        if (c := candidates[int(part) - 1]) not in chosen:
            chosen.append(c)
    return chosen


def _choose(candidates: list[FeedInfo], yes: bool) -> FeedInfo:
    if len(candidates) == 1 or yes:
        return candidates[0]
    for i, c in enumerate(candidates, start=1):
        click.echo(safe(f"{i}. {c.title}  ({c.feed_url}, {c.entries} entries)"))
    choice = click.prompt("Which feed?", type=click.IntRange(1, len(candidates)), default=1)
    return candidates[choice - 1]


@sources_group.command("test")
@click.argument("source_id")
def test_cmd(source_id: str) -> None:
    """Fetch a source once and show what it returns (nothing is stored)."""
    config, conn = _open()
    try:
        record = SourcesRepo(conn).get(source_id)
    finally:
        conn.close()
    if record is None:
        raise click.ClickException(f"no source {source_id!r}")

    async def go():
        async with PoliteClient(config.http) as http:
            return await adapter_for(record.source).fetch(record.source, FetchState(), http)

    try:
        result = asyncio.run(go())
    except (HttpError, AdapterError) as e:
        raise click.ClickException(safe(e)) from e
    except Exception as e:  # e.g. a 200 that isn't the JSON the adapter expects
        message = f"couldn't read the response: {type(e).__name__}: {e}"
        raise click.ClickException(safe(message)) from e
    click.echo(f"{source_id}: {len(result.items)} items ({result.skipped} skipped)")
    for item in result.items[:3]:
        click.echo(safe(f"  · {item.title}"))


def _set_enabled(source_id: str, enabled: bool) -> None:
    _, conn = _open()
    try:
        if not SourcesRepo(conn).set_enabled(source_id, enabled):
            raise click.ClickException(f"no source {source_id!r}")
    finally:
        conn.close()
    click.echo(f"{source_id} {'enabled' if enabled else 'disabled'}")


@sources_group.command("enable")
@click.argument("source_id")
def enable(source_id: str) -> None:
    """Include a source in scouts."""
    _set_enabled(source_id, True)


@sources_group.command("disable")
@click.argument("source_id")
def disable(source_id: str) -> None:
    """Skip a source in scouts (its items stay)."""
    _set_enabled(source_id, False)


@sources_group.command("remove")
@click.argument("source_id")
@click.option("--yes", "-y", is_flag=True, help="Don't ask for confirmation.")
def remove(source_id: str, yes: bool) -> None:
    """Remove a user-added source and its items."""
    _, conn = _open()
    try:
        if not yes and not click.confirm(f"Remove {source_id} and all its items?", default=False):
            raise click.Abort()
        try:
            found = SourcesRepo(conn).remove(source_id)
        except BuiltinSourceError as e:
            raise click.ClickException(str(e)) from e
        if not found:
            raise click.ClickException(f"no source {source_id!r}")
    finally:
        conn.close()
    click.echo(f"Removed {source_id}")
