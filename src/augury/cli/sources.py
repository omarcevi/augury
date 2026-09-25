import asyncio
import sqlite3

import click

from augury.core.clock import utcnow
from augury.core.config import Config, ConfigError, load_config
from augury.core.db.open import open_db
from augury.core.db.sources_repo import BuiltinSourceError, SourcesRepo
from augury.core.models import FetchState, RssRecipe, Source
from augury.core.paths import app_paths
from augury.core.text import slugify
from augury.sources.base import AdapterError
from augury.sources.http import HttpError, PoliteClient
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
            f"{(r.last_error or '')[:60]}"
        )


def add_feed_source(
    conn: sqlite3.Connection, info: FeedInfo, *, name: str | None, added_via: str, yes: bool
) -> Source:
    repo = SourcesRepo(conn)
    display = name or info.title
    source = Source.model_validate(
        {
            "id": repo.unique_id(slugify(display)),
            "name": display,
            "homepage": info.homepage,
            "origin": "user",
            "recipe": RssRecipe(feed_url=info.feed_url),
            "added_via": added_via,
        }
    )
    newest = f", newest {info.newest:%Y-%m-%d}" if info.newest else ""
    click.echo(f"{display}  ({info.feed_url})\n  {info.entries} entries{newest}")
    for title in info.sample_titles:
        click.echo(f"  · {title}")
    if not yes and not click.confirm(f"Add as {source.id!r}?", default=True):
        raise click.Abort()
    repo.add(source, now=utcnow())
    click.echo(f"Added {source.id}. It will be fetched on the next scout.")
    return source


@sources_group.command("add")
@click.option("--rss", "feed_url", metavar="URL", help="Add this RSS/Atom feed directly.")
@click.option("--name", help="Display name (defaults to the feed's title).")
@click.option("--yes", "-y", is_flag=True, help="Don't ask for confirmation.")
def add(feed_url: str | None, name: str | None, yes: bool) -> None:
    """Add a source."""
    if not feed_url:
        raise click.UsageError("pass --rss URL")
    config, conn = _open()
    try:
        repo = SourcesRepo(conn)
        if existing := repo.find_by_feed_url(feed_url):
            raise click.ClickException(f"that feed is already added as {existing!r}")

        async def go() -> FeedInfo:
            async with PoliteClient(config.http) as http:
                return await inspect_feed(feed_url, http)

        try:
            info = asyncio.run(go())
        except (HttpError, AdapterError) as e:
            raise click.ClickException(str(e)) from e
        if existing := repo.find_by_feed_url(info.feed_url):
            raise click.ClickException(f"that feed is already added as {existing!r}")
        add_feed_source(conn, info, name=name, added_via="manual", yes=yes)
    finally:
        conn.close()


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
        raise click.ClickException(str(e)) from e
    click.echo(f"{source_id}: {len(result.items)} items ({result.skipped} skipped)")
    for item in result.items[:3]:
        click.echo(f"  · {item.title}")


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
