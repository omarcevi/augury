"""`augury reindex` (spec §6.4, §10): chunks_fts and chunks_vec rebuilt from `chunks`. The
keyword index is rebuilt whole. The vector index is recreated when its size no longer matches
[embeddings] dimensions (or with `everything`), and every passage without a vector from the
configured embedder is embedded, 100 at a time, as one `embed` run under the daily budget. A
run the budget stops leaves the index mixed (queries stay refused); running it again carries
on where it stopped instead of starting over."""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from itertools import batched

from pydantic import BaseModel

from augury.agents.llm_step import describe, is_budget_stop
from augury.core.config import Config
from augury.core.db.chunks_repo import ChunksRepo, create_vec_table, vec_dimensions, vec_loaded
from augury.core.db.connect import transaction
from augury.core.db.runs_repo import RunsRepo, RunStatus
from augury.llm.embedder import EMBED_BATCH, Embedder, EmbedMeter
from augury.rag.guard import NO_VEC


class ReindexStats(BaseModel):
    chunks: int = 0  # passages in the keyword index
    embedded: int = 0
    remaining: int = 0  # passages still without a vector (after a stop)
    recreated: bool = False  # the vector index was made again (new size, or everything)
    keyword_only: str | None = None  # why there are no vectors at all
    stopped: str | None = None


async def reindex(
    conn: sqlite3.Connection,
    *,
    config: Config,
    embedder: Embedder | None,
    now: Callable[[], datetime],
    everything: bool = False,
    unavailable: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ReindexStats:
    repo = ChunksRepo(conn)
    with transaction(conn):
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    stats = ReindexStats(chunks=repo.count())
    if embedder is None:
        stats.keyword_only = unavailable or "no embedder is configured"
        return stats
    if not vec_loaded(conn):
        stats.keyword_only = NO_VEC
        return stats
    current = vec_dimensions(conn)
    with transaction(conn):
        if everything or (current is not None and current != embedder.dimensions):
            repo.clear_vectors()
            stats.recreated = True
        create_vec_table(conn, embedder.dimensions)
        repo.prune_vectors()
    todo = repo.needing_vectors(embedder.spec, embedder.dimensions)
    runs = RunsRepo(conn)
    run_id = runs.start("embed", now=now())
    meter = EmbedMeter(conn, config, run_id, now)
    status: RunStatus = "ok"
    try:
        for batch in batched(todo, EMBED_BATCH, strict=False):
            chunks = repo.get_many(list(batch))
            ordered = [chunks[i] for i in batch if i in chunks]
            try:
                vectors = await meter.embed(embedder, [c.embed_text for c in ordered], "document")
            except Exception as exc:
                if not is_budget_stop(exc):
                    raise
                stats.stopped, status = describe(exc, 200), "partial"
                break
            with transaction(conn):
                repo.set_vectors(
                    list(zip(ordered, vectors, strict=True)),
                    embed_model=embedder.spec,
                    embed_dim=embedder.dimensions,
                )
            stats.embedded += len(ordered)
            if on_progress is not None:
                on_progress(stats.embedded, len(todo))
    except BaseException as exc:
        failed = isinstance(exc, Exception)
        runs.finish(
            run_id,
            "failed" if failed else "interrupted",
            now=now(),
            error=describe(exc, 300) if failed else None,
            stats={"reindex": stats.model_dump()},
        )
        raise
    stats.remaining = len(todo) - stats.embedded
    runs.finish(
        run_id, status, now=now(), error=stats.stopped, stats={"reindex": stats.model_dump()}
    )
    return stats
