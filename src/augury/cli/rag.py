"""M4's commands (spec §10): `augury reindex` and `augury eval retrieval`."""

import asyncio
import sqlite3
from pathlib import Path

import click

from augury.cli.output import safe
from augury.core.clock import utcnow
from augury.core.config import Config, ConfigError, load_config
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.connect import connect, transaction
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.paths import app_paths
from augury.llm.budget import BudgetExceeded
from augury.llm.embedder import (
    Embedder,
    EmbedderUnavailable,
    EmbedMeter,
    HashEmbedder,
    resolve_embedder,
)
from augury.rag.eval import EvalReport, NotEnoughPairs, format_table, run_eval, save
from augury.rag.ingest import ingest_archive
from augury.rag.reindex import reindex as reindex_chunks
from augury.rag.search import VectorsUnavailable

FAKE_DIMENSIONS = 64


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


@click.group("eval")
def eval_group() -> None:
    """Measure augury's retrieval (spec §6.6)."""


async def _fake_index(
    conn: sqlite3.Connection, config: Config
) -> tuple[sqlite3.Connection, Embedder]:
    """A scratch in-memory copy whose archive is re-embedded by the hash embedder: the real
    database and its index are never touched, and nothing is spent."""
    scratch = connect(":memory:")
    conn.backup(scratch)
    with transaction(scratch):
        ChunksRepo(scratch).clear_vectors()
    embedder = HashEmbedder(FAKE_DIMENSIONS)
    run_id = RunsRepo(scratch).start("embed", now=utcnow())
    meter = EmbedMeter(scratch, config, run_id, utcnow)
    await ingest_archive(scratch, embedder=embedder, meter=meter, now=utcnow)
    return scratch, embedder


async def _evaluate(
    conn: sqlite3.Connection, config: Config, embedder: Embedder | None, fake: bool, why: str | None
) -> EvalReport:
    if fake:
        conn, embedder = await _fake_index(conn, config)
    if embedder is None:
        raise click.ClickException(
            f"the eval needs an embedder: {why}. `--fake-embedder` checks the code path offline"
        )
    runs = RunsRepo(conn)
    run_id = runs.start("embed", now=utcnow())
    try:
        report = await run_eval(
            conn, embedder=embedder, meter=EmbedMeter(conn, config, run_id, utcnow), now=utcnow()
        )
    except (NotEnoughPairs, VectorsUnavailable, BudgetExceeded) as e:
        runs.finish(run_id, "failed", now=utcnow(), error=str(e))
        raise click.ClickException(str(e)) from e
    runs.finish(run_id, "ok", now=utcnow(), stats={"eval": report.model_dump(mode="json")})
    return report


@eval_group.command("retrieval")
@click.option(
    "--fake-embedder",
    is_flag=True,
    help="Offline and free: a hash embedder over a scratch copy (checks the code, not quality).",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("evals/results"),
    show_default=True,
    help="Folder for the JSON results.",
)
def retrieval(fake_embedder: bool, out_dir: Path) -> None:
    """recall@1/5/10 and MRR for keyword, vector and hybrid search on blog↔paper pairs."""
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    embedder, why = (None, None) if fake_embedder else configured_embedder()
    conn = open_db(paths)
    try:
        report = asyncio.run(_evaluate(conn, config, embedder, fake_embedder, why))
    finally:
        conn.close()
    click.echo(format_table(report))
    click.echo(f"Saved {save(report, out_dir)}")
