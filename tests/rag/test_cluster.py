import json
from datetime import timedelta

from augury.agents.rank import build_digest
from augury.core.clock import local_day
from augury.core.config import Config, RankingConfig
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.state_repo import StateRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.cluster import cluster_siblings, novelty_scores, update_clusters
from augury.rag.ingest import ingest_archive
from tests.rag.helpers import NOW, add_items

SAME = "Speculative decoding with draft models doubles throughput"
LINK = (
    0.6  # the hash embedder's near-duplicates score ~0.7-0.8; the default 0.85 is for real models
)


async def _ingest(conn, threshold: float | None = LINK, embedder=None):
    meter = EmbedMeter(conn, Config(), RunsRepo(conn).start("scout", now=NOW), lambda: NOW)
    return await ingest_archive(
        conn,
        embedder=embedder or HashEmbedder(64),
        meter=meter,
        now=lambda: NOW,
        cluster_threshold=threshold,
    )


def _cluster(conn, item_id: str) -> str | None:
    return conn.execute("SELECT cluster_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]


async def test_near_duplicates_from_two_sources_share_a_cluster(paths):
    conn = open_db(paths, now=NOW)
    [blog] = add_items(conn, [(SAME, "the same story")], source_id="hf-blog")
    [post] = add_items(conn, [(SAME, "the same story")], source_id="hf-community")
    [other] = add_items(conn, [("Tomato soup", "a recipe")], source_id="hf-community")
    stats = await _ingest(conn)
    assert stats.clustered == 2
    assert _cluster(conn, blog) == _cluster(conn, post) == min(blog, post)
    assert _cluster(conn, other) is None  # alone: no cluster
    assert [s.item_id for s in cluster_siblings(conn, blog)] == [post]


async def test_a_blog_always_joins_the_paper_it_names(paths):
    conn = open_db(paths, now=NOW)
    [paper] = add_items(
        conn,
        [("A paper title", "abstract")],
        source_id="hf-papers",
        kind="paper",
        arxiv_ids=["2609.00001"],
    )
    [blog] = add_items(conn, [("Unrelated words", "nothing alike")], arxiv_ids=["2609.00001"])
    await _ingest(conn, embedder=None, threshold=0.99)  # keyword-only: the arXiv link still holds
    assert _cluster(conn, blog) == _cluster(conn, paper) is not None


async def test_items_outside_the_14_day_window_never_cluster(paths):
    conn = open_db(paths, now=NOW)
    [old] = add_items(conn, [(SAME, "x")], published_at=NOW - timedelta(days=30))
    [new] = add_items(conn, [(SAME, "x")], source_id="hf-community", published_at=NOW)
    await _ingest(conn)
    assert _cluster(conn, old) is None and _cluster(conn, new) is None


async def test_joining_an_existing_cluster_keeps_its_id(paths):
    conn = open_db(paths, now=NOW)
    first = add_items(conn, [(SAME, "x")]) + add_items(conn, [(SAME, "x")], source_id="hf-papers")
    await _ingest(conn)
    cluster = _cluster(conn, first[0])
    assert cluster is not None
    [late] = add_items(conn, [(SAME, "x")], source_id="hf-community")
    await _ingest(conn)
    assert _cluster(conn, late) == cluster
    assert update_clusters(conn, [late], threshold=LINK) == 0  # idempotent


async def test_novelty_needs_something_read_in_the_last_60_days(paths):
    conn = open_db(paths, now=NOW)
    [read, similar, different] = add_items(
        conn, [(SAME, "x"), (SAME + " again", "x"), ("Tomato soup recipe", "cooking")]
    )
    await _ingest(conn, threshold=None)
    today = local_day(NOW)
    assert novelty_scores(conn, [similar, different], today=today) == {}  # nothing read yet
    StateRepo(conn).mark_opened(read, now=NOW)
    scores = novelty_scores(conn, [similar, different], today=today)
    assert scores[similar] < 0.5 < scores[different]
    later = today + timedelta(days=61)
    assert novelty_scores(conn, [similar], today=later) == {}  # read too long ago


async def test_the_ai_digest_weighs_novelty(paths):
    conn = open_db(paths, now=NOW)
    [read, similar, different] = add_items(
        conn, [(SAME, "x"), (SAME + " again", "x"), ("Tomato soup recipe", "cooking")]
    )
    await _ingest(conn, threshold=None)
    StateRepo(conn).mark_opened(read, now=NOW)
    only_novelty = RankingConfig(w_rel=0, w_pop=0, w_rec=0, w_nov=1)
    build_digest(conn, local_day(NOW), now=NOW, config=only_novelty, ai=True)
    rows = conn.execute("SELECT item_id, breakdown_json FROM digests ORDER BY position")
    breakdowns = {r[0]: json.loads(r[1]) for r in rows}
    assert breakdowns[different]["terms"]["nov"] > breakdowns[similar]["terms"]["nov"]
    assert "nov" in breakdowns[read]["missing"] or "nov" in breakdowns[read]["terms"]
