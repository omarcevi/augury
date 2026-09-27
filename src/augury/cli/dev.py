import asyncio

import click

from augury.agents.discovery.transcript import transcript
from augury.cli.output import safe
from augury.core.clock import utcnow
from augury.core.config import ConfigError, load_config
from augury.core.db.discovery_repo import DiscoveryRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.models import Content
from augury.core.paths import app_paths
from augury.extract.service import get_or_extract
from augury.sources.http import PoliteClient


@click.group("dev", hidden=True)
def dev_group() -> None:
    """Developer checks. Not part of the documented CLI."""


@dev_group.command("extract")
@click.argument("item_id")
@click.option("--refresh", is_flag=True, help="Ignore the cached copy.")
def extract_cmd(item_id: str, refresh: bool) -> None:
    """Print an item's extracted markdown."""
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    conn = open_db(paths)
    try:
        item = ItemsRepo(conn).get(item_id)
        if item is None:
            raise click.ClickException(f"no item {item_id!r}; run `augury scout` first")

        async def go() -> Content:
            async with PoliteClient(config.http) as http:
                return await get_or_extract(conn, http, item, now=utcnow(), refresh=refresh)

        content = asyncio.run(go())
    finally:
        conn.close()
    error = f" · {content.error}" if content.error else ""
    summary = f"[{content.status}] {content.extractor} · {content.word_count} words{error}"
    click.echo(safe(summary), err=True)
    if content.status != "ok":
        raise SystemExit(1)
    click.echo(safe(content.body_md))


@dev_group.command("discovery")
@click.argument("run_id")
def discovery_cmd(run_id: str) -> None:
    """Replay a discovery run's transcript from sessions.db."""
    paths = app_paths()
    conn = open_db(paths)
    try:
        run = DiscoveryRepo(conn).get(run_id)
    finally:
        conn.close()
    if run is None:
        raise click.ClickException(f"no discovery run {run_id!r}")
    click.echo(safe(f"{run.id}: {run.query!r} ({run.input_kind}) · {run.status}"))
    click.echo(f"{run.tool_calls} tool calls · {run.tokens_in} in / {run.tokens_out} out tokens")
    if run.explanation:
        click.echo(safe(f"note: {run.explanation}"))
    lines = asyncio.run(transcript(paths.sessions_db_file, run.session_id or run.id))
    if lines is None:
        raise click.ClickException("its transcript is not in sessions.db")
    for line in lines:
        click.echo(safe(line))
