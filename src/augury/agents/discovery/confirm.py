"""The only way a discovered candidate becomes a source: the user confirmed it (spec §5.6)."""

import sqlite3
from datetime import datetime

from augury.agents.discovery.guards import homepage_for
from augury.agents.discovery.models import Candidate, primary_url
from augury.core.db.connect import transaction
from augury.core.db.discovery_repo import DiscoveryRepo
from augury.core.db.sources_repo import SourceExists, SourcesRepo
from augury.core.models import Source
from augury.core.text import slugify


class DuplicateCandidate(Exception):
    pass


def add_candidates(
    conn: sqlite3.Connection,
    chosen: list[Candidate],
    *,
    run_id: str | None,
    now: datetime,
) -> list[Source]:
    """Adds each chosen candidate as a new user source (added_via discovery) and records the
    choice on its discovery run. Duplicates are skipped: their URL is already a source. All or
    nothing: if any insert fails, no source is added and the error propagates."""
    repo = SourcesRepo(conn)
    added: list[Source] = []
    with transaction(conn):
        for c in chosen:
            if c.duplicate_of or repo.find_by_recipe_url(_url(c)):
                continue
            source = Source(
                id=repo.unique_id(slugify(c.name)),
                name=c.name,
                # checked again here: a candidate may come back from discovery_runs' JSON
                homepage=homepage_for(c.recipe, c.homepage),
                origin="user",
                recipe=c.recipe,
                added_via="discovery",
            )
            try:
                repo.add(source, now=now, discovery_run_id=run_id)
            except SourceExists:
                continue
            added.append(source)
        if run_id is not None:
            DiscoveryRepo(conn).set_chosen(run_id, [c.recipe_hash for c in chosen])
    return added


def replace_with(
    conn: sqlite3.Connection, source_id: str, candidate: Candidate, *, run_id: str | None
) -> bool:
    """Re-discover: the broken source keeps its id and items and gets the candidate's recipe."""
    if (other := SourcesRepo(conn).find_by_recipe_url(_url(candidate))) and other != source_id:
        raise DuplicateCandidate(f"that URL is already fetched by {other}")
    done = SourcesRepo(conn).replace_recipe(source_id, candidate.recipe, discovery_run_id=run_id)
    if done and run_id is not None:
        DiscoveryRepo(conn).set_chosen(run_id, [candidate.recipe_hash])
    return done


def _url(c: Candidate) -> str:
    return primary_url(c.recipe)
