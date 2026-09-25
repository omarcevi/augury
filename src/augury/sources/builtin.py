import sqlite3
from datetime import datetime

from augury.core.db.sources_repo import SourcesRepo
from augury.core.models import HfBlogRecipe, HfCommunityRecipe, HfPapersRecipe, Source

BUILTIN_SOURCES: tuple[Source, ...] = (
    Source(
        id="hf-papers",
        name="Hugging Face Papers",
        homepage="https://huggingface.co/papers",
        origin="builtin",
        recipe=HfPapersRecipe(),
        added_via="builtin",
    ),
    Source(
        id="hf-blog",
        name="Hugging Face Blog",
        homepage="https://huggingface.co/blog",
        origin="builtin",
        recipe=HfBlogRecipe(),
        added_via="builtin",
    ),
    Source(
        id="hf-community",
        name="Hugging Face Community",
        homepage="https://huggingface.co/blog/community",
        origin="builtin",
        recipe=HfCommunityRecipe(),
        added_via="builtin",
        trust="community",
    ),
)


def seed_builtin_sources(conn: sqlite3.Connection, *, now: datetime) -> None:
    repo = SourcesRepo(conn)
    for source in BUILTIN_SOURCES:
        repo.ensure(source, now=now)  # INSERT OR IGNORE: a user's "disabled" survives restarts
