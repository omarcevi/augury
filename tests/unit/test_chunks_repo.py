import sqlite_vec

from augury.core.clock import local_day
from augury.core.db.chunks_repo import ChunksRepo, NewChunk, vec_dimensions
from augury.core.db.connect import connect, transaction
from augury.core.db.migrate import available_migrations, migrate
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.state_repo import StateRepo
from tests.rag.helpers import NOW, add_items


def _chunk(item_id: str, text: str, collection: str = "archive") -> NewChunk:
    return NewChunk(
        item_id=item_id,
        collection=collection,
        source_id="hf-blog",
        kind="article",
        trust="publisher",
        published_day=20260927,
        section="",
        char_start=0,
        char_end=len(text),
        text=text,
        context_header="HF Blog · T",
        content_hash="h",
        chunker_version=1,
    )


def _fts(conn, query: str) -> list[int]:
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (query,))
    return [r[0] for r in rows]


def _knn(conn, vector, k=5, where="") -> list[int]:
    rows = conn.execute(
        f"SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ?{where}",
        (sqlite_vec.serialize_float32(vector), k),
    )
    return [r[0] for r in rows]


def test_migration_005_adds_the_rag_tables_and_the_embed_run_kind(paths):
    conn = open_db(paths, now=NOW)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"chunks", "chunks_fts", "asks"} <= tables
    assert "chunks_vec" not in tables  # made on first use, at the embedder's size
    assert "cluster_id" in {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    RunsRepo(conn).start("embed", now=NOW)  # the CHECK now allows it


def test_v4_runs_survive_the_runs_rebuild(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn, available_migrations()[:4])
    conn.execute(
        "INSERT INTO runs (id, kind, started_at, status, tokens_in, cost_usd, unpriced_tokens)"
        " VALUES ('d1', 'discovery', '2026-09-26T00:00:00+00:00', 'ok', 9, 0.25, 3)"
    )
    migrate(conn)
    row = conn.execute("SELECT kind, tokens_in, cost_usd, unpriced_tokens FROM runs")
    assert tuple(row.fetchone()) == ("discovery", 9, 0.25, 3)


def test_replace_writes_rows_keywords_and_vectors_in_one_go(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("T", "s")])
    repo = ChunksRepo(conn)
    with transaction(conn):
        ids = repo.replace(
            item,
            "archive",
            [_chunk(item, "speculative decoding")],
            [[1.0, 0.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    assert _fts(conn, "speculative") == ids and _knn(conn, [1.0, 0.0]) == ids
    assert vec_dimensions(conn) == 2 and repo.index_models() == [("hash/x", 2)]
    with transaction(conn):  # again: the old passage, its keywords and its vector are gone
        new = repo.replace(
            item,
            "archive",
            [_chunk(item, "quantization")],
            [[0.0, 1.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    assert _fts(conn, "speculative") == [] and _fts(conn, "quantization") == new
    assert _knn(conn, [1.0, 0.0]) == new and repo.count() == 1


def test_passages_without_vectors_are_keyword_only(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("T", "s")])
    repo = ChunksRepo(conn)
    with transaction(conn):
        repo.replace(
            item,
            "archive",
            [_chunk(item, "rope scaling")],
            None,
            embed_model=None,
            embed_dim=None,
            now=NOW,
        )
    assert len(_fts(conn, "rope")) == 1
    assert vec_dimensions(conn) is None and repo.index_models() == []
    assert repo.needing_vectors("hash/x", 2) == [c.id for c in repo.for_item(item, "archive")]


def test_the_vector_table_of_another_size_is_replaced_only_when_empty(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("T", "s")])
    repo = ChunksRepo(conn)
    repo.ensure_vec_table(4)
    repo.ensure_vec_table(2)  # nothing stored yet: recreated at the new size
    assert vec_dimensions(conn) == 2
    with transaction(conn):
        repo.replace(
            item,
            "archive",
            [_chunk(item, "x")],
            [[1.0, 0.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    try:
        repo.ensure_vec_table(8)
    except ValueError as e:
        assert "augury reindex" in str(e)
    else:
        raise AssertionError("a size change with stored vectors must be refused")


def test_first_read_sets_read_day_inside_the_vector_index(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("T", "s")])
    with transaction(conn):
        ChunksRepo(conn).replace(
            item,
            "archive",
            [_chunk(item, "x")],
            [[1.0, 0.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    day = local_day(NOW)
    number = day.year * 10_000 + day.month * 100 + day.day
    assert _knn(conn, [1.0, 0.0], where=" AND read_day >= 1") == []
    StateRepo(conn).mark_opened(item, now=NOW)
    [hit] = _knn(conn, [1.0, 0.0], where=f" AND read_day = {number}")
    assert hit == ChunksRepo(conn).for_item(item, "archive")[0].id


def test_vectors_of_removed_items_are_pruned(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("T", "s")])
    repo = ChunksRepo(conn)
    with transaction(conn):
        repo.replace(
            item,
            "archive",
            [_chunk(item, "x")],
            [[1.0, 0.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    conn.execute("DELETE FROM items WHERE id = ?", (item,))  # cascades to chunks and FTS
    assert repo.count() == 0 and _fts(conn, "x") == []
    assert repo.prune_vectors() == 1 and _knn(conn, [1.0, 0.0]) == []


def test_a_new_passage_never_takes_the_id_of_a_removed_items_vector(paths):
    conn = open_db(paths, now=NOW)
    first, second = add_items(conn, [("A", "a"), ("B", "b")])
    repo = ChunksRepo(conn)
    with transaction(conn):
        for item in (first, second):
            repo.replace(
                item,
                "archive",
                [_chunk(item, "x")],
                [[1.0, 0.0]],
                embed_model="hash/x",
                embed_dim=2,
                now=NOW,
            )
    conn.execute("DELETE FROM items WHERE id = ?", (second,))  # its vector waits for a prune
    with transaction(conn):  # before any prune, like opening an item or asking about it
        [new] = repo.replace(
            first,
            "content",
            [_chunk(first, "y", "content")],
            [[0.0, 1.0]],
            embed_model="hash/x",
            embed_dim=2,
            now=NOW,
        )
    assert repo.prune_vectors() == 1 and _knn(conn, [0.0, 1.0], k=1) == [new]
