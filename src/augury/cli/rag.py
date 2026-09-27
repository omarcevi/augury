"""M4's commands (spec §10): `augury reindex` and `augury eval retrieval`."""

import asyncio

import click

from augury.cli.output import safe
from augury.core.clock import utcnow
from augury.core.config import ConfigError, load_config
from augury.core.db.open import open_db
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.paths import app_paths
from augury.llm.embedder import Embedder, EmbedderUnavailable, resolve_embedder
from augury.rag.reindex import reindex as reindex_chunks


def configured_embedder() -> tuple[Embedder | None, str | None]:
    try:
        return resolve_embedder(load_config(app_paths())), None
    except EmbedderUnavailable as e:
        return None, str(e)


@click.command()
@click.option(
    "--all", "everything", is_flag=True, help="Embed every passage again, not only the missing."
)
def reindex(everything: bool) -> None:
    """Rebuild the keyword and vector indexes from the stored passages."""
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    embedder, why = configured_embedder()
    conn = open_db(paths)
    lock = ScoutLock(paths.scout_lock_file)  # a scout ingesting meanwhile would mix the index
    try:
        lock.acquire()
    except ScoutAlreadyRunning as e:
        conn.close()
        raise click.ClickException(f"{e}; run reindex when it has finished") from e
    try:
        stats = asyncio.run(
            reindex_chunks(
                conn,
                config=config,
                embedder=embedder,
                now=utcnow,
                everything=everything,
                unavailable=why,
            )
        )
    finally:
        lock.release()
        conn.close()
    click.echo(f"Keyword index rebuilt: {stats.chunks} passages.")
    if stats.keyword_only:
        click.echo(f"No vectors: {safe(stats.keyword_only)}. Search stays keyword-only.")
        return
    assert embedder is not None
    made = "recreated, " if stats.recreated else ""
    click.echo(
        f"Vector index ({made}{embedder.spec}, {embedder.dimensions} dims):"
        f" {stats.embedded} passages embedded."
    )
    if stats.stopped:
        click.echo(
            f"Stopped: {safe(stats.stopped)}. {stats.remaining} passages still need a vector;"
            " run `augury reindex` again to carry on."
        )
        raise SystemExit(1)
