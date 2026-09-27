import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.genai import types

from augury.core.config import BudgetConfig, Config, ConfigError, EmbeddingsConfig, validate_config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm import embedder as embedder_module
from augury.llm.budget import BudgetExceeded
from augury.llm.embedder import (
    EmbedderUnavailable,
    EmbeddingError,
    EmbedMeter,
    GenAIEmbedder,
    HashEmbedder,
    LiteLLMEmbedder,
    hash_vector,
    index_problem,
    resolve_embedder,
)

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
KEY = {"GEMINI_API_KEY": "test-key"}


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hash_vectors_are_deterministic_normalized_and_lexical():
    a = hash_vector("sparse attention for long context", 64)
    assert a == hash_vector("sparse attention for long context", 64)
    assert math.isclose(sum(x * x for x in a), 1.0)
    near = hash_vector("long context sparse attention kernels", 64)
    far = hash_vector("tomato soup recipe", 64)
    assert _cos(a, near) > _cos(a, far)


async def test_hash_embedder_spends_nothing_and_records_calls():
    e = HashEmbedder(dimensions=16)
    out = await e.embed(["a", "b"], "query")
    assert len(out.vectors) == 2 and all(len(v) == 16 for v in out.vectors)
    assert out.tokens == 0 and e.calls == [("query", 2)]


def test_embeddings_config_defaults_and_validation(tmp_path):
    c = Config()
    assert c.embeddings.model == "gemini/gemini-embedding-001" and c.embeddings.dimensions == 768
    assert c.rag.cluster_threshold == 0.85
    assert EmbeddingsConfig(model="").model == ""  # embeddings off
    with pytest.raises(ConfigError, match="provider/model"):
        validate_config({"embeddings": {"model": "nomodel"}}, tmp_path / "config.toml")
    with pytest.raises(ConfigError, match="dimensions"):
        validate_config({"embeddings": {"dimensions": 0}}, tmp_path / "config.toml")


def test_no_key_no_embedder():
    with pytest.raises(EmbedderUnavailable, match="GEMINI_API_KEY"):
        resolve_embedder(Config(), env={})
    with pytest.raises(EmbedderUnavailable, match="off"):
        resolve_embedder(Config(embeddings=EmbeddingsConfig(model="")), env=KEY)
    vertex = Config(embeddings=EmbeddingsConfig(model="vertex_ai/gemini-embedding-001"))
    with pytest.raises(EmbedderUnavailable, match="project"):
        resolve_embedder(vertex, env={})


def test_gemini_resolves_to_the_genai_embedder():
    e = resolve_embedder(Config(), env=KEY)
    assert isinstance(e, GenAIEmbedder)
    assert (e.spec, e.dimensions, e.native, e.name) == (
        "gemini/gemini-embedding-001",
        768,
        True,
        "gemini-embedding-001",
    )


class FakeModels:
    def __init__(self, dims: int, count: int | None = None) -> None:
        self.dims, self.count = dims, count
        self.calls: list[dict[str, Any]] = []

    async def embed_content(self, *, model: str, contents: Any, config: Any):
        self.calls.append({"model": model, "contents": contents, "config": config})
        n = len(contents) if self.count is None else self.count
        return types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=[3.0, 4.0] + [0.0] * (self.dims - 2))] * n
        )


def _genai(dims: int = 4, *, count: int | None = None, one_per_call: bool = False):
    models = FakeModels(dims, count)
    client = cast(Any, SimpleNamespace(aio=SimpleNamespace(models=models)))
    embedder = GenAIEmbedder(
        "gemini/gemini-embedding-001", client, dimensions=4, one_per_call=one_per_call
    )
    return embedder, models


async def test_genai_sends_one_content_per_text_with_task_type_and_size():
    embedder, models = _genai()
    out = await embedder.embed(["first text", "second"], "document")
    call = models.calls[0]
    assert call["model"] == "gemini-embedding-001"
    assert [c.parts[0].text for c in call["contents"]] == ["first text", "second"]
    assert call["config"].task_type == "RETRIEVAL_DOCUMENT"
    assert call["config"].output_dimensionality == 4
    assert out.vectors[0] == [0.6, 0.8, 0.0, 0.0]  # L2-normalized
    assert out.tokens == math.ceil(10 / 4) + math.ceil(6 / 4)  # estimated: the API reports none
    await embedder.embed(["q"], "query")
    assert models.calls[1]["config"].task_type == "RETRIEVAL_QUERY"


async def test_vertex_embeds_one_text_per_call():
    embedder, models = _genai(one_per_call=True)
    await embedder.embed(["a", "b", "c"], "document")
    assert [len(c["contents"]) for c in models.calls] == [1, 1, 1]


async def test_a_wrong_count_or_size_is_an_error_that_names_the_fix():
    embedder, _ = _genai(count=1)
    with pytest.raises(EmbeddingError, match="1 vectors for 2 texts"):
        await embedder.embed(["a", "b"], "document")
    embedder, _ = _genai(dims=8)
    with pytest.raises(EmbeddingError, match="reindex"):
        await embedder.embed(["a"], "document")


async def test_litellm_embedder_uses_aembedding(monkeypatch):
    seen: dict[str, Any] = {}

    async def aembedding(**kwargs: Any):
        seen.update(kwargs)
        data = [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [2.0, 0.0]}]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=7))

    monkeypatch.setattr(
        embedder_module, "import_litellm", lambda: SimpleNamespace(aembedding=aembedding)
    )
    e = LiteLLMEmbedder("openai/text-embedding-3-small", dimensions=2)
    out = await e.embed(["x", "y"], "document")
    assert seen["model"] == "openai/text-embedding-3-small" and seen["input"] == ["x", "y"]
    assert seen["dimensions"] == 2
    assert out.vectors == [[1.0, 0.0], [0.0, 1.0]] and out.tokens == 7


def test_index_problem_is_the_mixed_model_guard():
    e = HashEmbedder(dimensions=16)
    assert index_problem([], e) is None
    assert index_problem([(e.spec, 16)], e) is None
    problem = index_problem([("gemini/gemini-embedding-001", 768)], e)
    assert problem is not None and "augury reindex" in problem
    assert index_problem([(e.spec, 16), (e.spec, 32)], e) is not None  # a size change too


class CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__(dimensions=4, spec="gemini/gemini-embedding-001")

    async def embed(self, texts, task):
        out = await super().embed(texts, task)
        return type(out)(out.vectors, tokens=10 * len(texts))


async def test_meter_batches_by_100_and_records_spend(paths):
    conn = open_db(paths, now=NOW)
    run_id = RunsRepo(conn).start("scout", now=NOW)
    embedder = CountingEmbedder()
    meter = EmbedMeter(conn, Config(), run_id, lambda: NOW)
    vectors = await meter.embed(embedder, [f"t{i}" for i in range(250)], "document")
    assert len(vectors) == 250 and [n for _, n in embedder.calls] == [100, 100, 50]
    row = conn.execute("SELECT tokens_in, tokens_out, cost_usd FROM runs WHERE id = ?", (run_id,))
    tokens_in, tokens_out, cost = row.fetchone()
    assert (tokens_in, tokens_out) == (2500, 0)
    assert math.isclose(cost, 2500 * 0.15 / 1_000_000)  # pricing.toml's embedding price


async def test_meter_refuses_before_calling_once_the_budget_is_spent(paths):
    conn = open_db(paths, now=NOW)
    run_id = RunsRepo(conn).start("scout", now=NOW)
    embedder = CountingEmbedder()
    config = Config(budget=BudgetConfig(daily_usd=0))
    with pytest.raises(BudgetExceeded):
        await EmbedMeter(conn, config, run_id, lambda: NOW).embed(embedder, ["a"], "query")
    assert embedder.calls == []
