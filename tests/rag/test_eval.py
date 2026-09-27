import json

import pytest
from click.testing import CliRunner

from augury.cli import main
from augury.core.config import Config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.eval import MIN_PAIRS, NotEnoughPairs, build_pairs, run_eval, save, score
from augury.rag.ingest import ingest_archive
from tests.rag.helpers import NOW, add_items

TOPICS = [
    "sparse attention",
    "speculative decoding",
    "mixture of experts",
    "state space models",
    "diffusion transformers",
    "reward modeling",
    "tool calling agents",
    "long context retrieval",
    "vision language models",
    "quantization kernels",
    "kv cache compression",
    "world models",
    "test time compute",
    "synthetic data",
    "preference optimization",
    "code generation",
    "audio tokenizers",
    "robot policies",
    "graph neural networks",
    "federated learning",
    "protein folding",
    "weather forecasting",
    "text to video",
    "model merging",
    "curriculum learning",
    "memory layers",
    "retrieval augmentation",
    "chain of thought",
    "multilingual pretraining",
    "embedding models",
    "reasoning traces",
    "safety evals",
]


def seed_pairs(conn, n: int = MIN_PAIRS) -> None:
    ids = [f"2609.{i:05d}" for i in range(n)]
    add_items(
        conn,
        [(f"{t.title()}: a new method", f"We study {t}.") for t in TOPICS[:n]],
        source_id="hf-papers",
        kind="paper",
        arxiv_ids=ids,
        now=NOW,
    )
    add_items(
        conn,
        [(f"What {t} means for you", f"A blog post about {t} in practice.") for t in TOPICS[:n]],
        arxiv_ids=ids,
        now=NOW,
    )


async def _indexed(paths, n: int = MIN_PAIRS):
    conn = open_db(paths, now=NOW)
    seed_pairs(conn, n)
    embedder = HashEmbedder(64)
    meter = EmbedMeter(conn, Config(), RunsRepo(conn).start("embed", now=NOW), lambda: NOW)
    await ingest_archive(conn, embedder=embedder, meter=meter, now=lambda: NOW)
    return conn, embedder, meter


def test_scores_are_recall_at_k_and_mrr():
    s = score([1, 3, None, 10])
    assert s.recall == {1: 0.25, 5: 0.5, 10: 0.75}
    assert s.mrr == pytest.approx((1 + 1 / 3 + 1 / 10) / 4)


async def test_pairs_come_from_blogs_that_name_a_paper(paths):
    conn, _, _ = await _indexed(paths)
    pairs = build_pairs(conn)
    assert len(pairs) == MIN_PAIRS
    assert all(p.blog_id.startswith("web:") and p.paper_id.startswith("arxiv:") for p in pairs)
    assert pairs[0].query.startswith("What ")


async def test_the_eval_reports_every_mode_and_saves_json(paths, tmp_path):
    conn, embedder, meter = await _indexed(paths)
    report = await run_eval(conn, embedder=embedder, meter=meter, now=NOW)
    assert set(report.modes) == {"fts", "vector", "hybrid"} and report.pairs == MIN_PAIRS
    for scores in report.modes.values():
        assert set(scores.recall) == {1, 5, 10} and 0 <= scores.mrr <= 1
    assert report.modes["hybrid"].recall[10] > 0.5  # the topic words are shared
    path = save(report, tmp_path / "results")
    assert path.name == f"20260927T090000-{report.config_hash}.json"
    assert json.loads(path.read_text())["pairs"] == MIN_PAIRS


async def test_too_few_pairs_says_how_many_there_are(paths):
    conn, embedder, meter = await _indexed(paths, n=5)
    with pytest.raises(NotEnoughPairs, match="only 5 blog↔paper pairs"):
        await run_eval(conn, embedder=embedder, meter=meter, now=NOW)


def test_the_cli_runs_offline_with_the_fake_embedder(paths, tmp_path):
    conn = open_db(paths, now=NOW)
    seed_pairs(conn)
    conn.close()
    out = tmp_path / "results"
    result = CliRunner().invoke(main, ["eval", "retrieval", "--fake-embedder", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "30 blog↔paper pairs · hash/feature-hashing (64 dims)" in result.output
    assert "hybrid" in result.output and len(list(out.glob("*.json"))) == 1
    conn = open_db(paths, now=NOW)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0  # scratch copy only


def test_the_cli_refuses_without_an_embedder_or_enough_pairs(paths, tmp_path):
    result = CliRunner().invoke(main, ["eval", "retrieval", "--out", str(tmp_path)])
    assert result.exit_code != 0 and "needs an embedder" in result.output
    result = CliRunner().invoke(
        main, ["eval", "retrieval", "--fake-embedder", "--out", str(tmp_path)]
    )
    assert result.exit_code != 0 and "only 0 blog↔paper pairs" in result.output
