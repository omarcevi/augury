"""Embeddings (spec §6.3), configured apart from the LLM roles in [embeddings]. `gemini/…` and
`vertex_ai/…` go through google-genai (task type RETRIEVAL_DOCUMENT when ingesting,
RETRIEVAL_QUERY when searching); every other provider through `litellm.aembedding` (the
`providers` extra). Vectors are cut to [embeddings] dimensions and L2-normalized.

Embedding calls don't pass through an ADK LlmAgent, so EmbedMeter does the budget's job for
them (spec §5.9): today's spend is checked before every batch of 100, and each batch's tokens
and cost are added to a run, like a model call's."""

import hashlib
import math
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import Any, Literal, Protocol

from google import genai
from google.genai import types

from augury.core.config import Config
from augury.core.db.runs_repo import RunsRepo
from augury.llm.budget import BudgetExceeded, budget_problem, today_spend
from augury.llm.pricing import price_for
from augury.llm.resolver import (
    GEMINI_KEY_VARS,
    LLM_RETRIES,
    PROVIDERS_HINT,
    RETRY_STATUSES,
    import_litellm,
)

EmbedTask = Literal["document", "query"]
EMBED_BATCH = 100  # spec §6.3; also the Gemini API's batchEmbedContents limit
_GENAI_TASK: dict[EmbedTask, str] = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}
_WORD = re.compile(r"[\w.\-]+")


class EmbedderUnavailable(Exception):
    """No embedder can run: search stays keyword-only (spec N2). The message says why."""


class EmbeddingError(Exception):
    """The provider answered, but not with one vector of the configured size per text."""


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    tokens: int  # input tokens, as the provider reports them or estimated (chars / 4)


class Embedder(Protocol):
    @property
    def spec(self) -> str: ...  # as written in [embeddings] model; stored with every chunk

    @property
    def dimensions(self) -> int: ...

    @property
    def native(self) -> bool: ...  # served by google-genai (prices come from pricing.toml)

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded: ...


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)  # spec §6.2's estimate


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else [0.0 for _ in vector]


def _checked(vectors: list[list[float]], texts: Sequence[str], spec: str, dims: int) -> None:
    if len(vectors) != len(texts):
        raise EmbeddingError(f"{spec} returned {len(vectors)} vectors for {len(texts)} texts")
    for v in vectors:
        if len(v) != dims:
            raise EmbeddingError(
                f"{spec} returned {len(v)}-dimension vectors, but [embeddings] dimensions is"
                f" {dims}: set dimensions = {len(v)} and run `augury reindex`"
            )


class GenAIEmbedder:
    def __init__(
        self, spec: str, client: genai.Client, *, dimensions: int, one_per_call: bool
    ) -> None:
        self._spec, self._dims, self.client = spec, dimensions, client
        self.name = spec.partition("/")[2]
        # Vertex's embedding endpoints take one text per request (google/genai/models.py,
        # embed_content), so there each text is its own call.
        self.one_per_call = one_per_call

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def native(self) -> bool:
        return True

    async def _call(self, texts: Sequence[str], task: EmbedTask) -> list[types.ContentEmbedding]:
        # One Content per text: a plain list of strings becomes ONE multi-part content for
        # gemini-embedding-2 (google/genai/_transformers.py t_contents), i.e. one vector.
        contents: Any = [types.Content(parts=[types.Part(text=t)]) for t in texts]
        config = types.EmbedContentConfig(
            task_type=_GENAI_TASK[task], output_dimensionality=self._dims
        )
        response = await self.client.aio.models.embed_content(
            model=self.name, contents=contents, config=config
        )
        return list(response.embeddings or [])

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded:
        found: list[types.ContentEmbedding] = []
        if self.one_per_call:
            for t in texts:
                found += await self._call([t], task)
        else:
            found = await self._call(texts, task)
        vectors = [normalize(e.values or []) for e in found]
        _checked(vectors, texts, self._spec, self._dims)
        tokens = 0
        for e, t in zip(found, texts, strict=True):
            counted = e.statistics.token_count if e.statistics else None  # Vertex only
            tokens += int(counted) if counted else estimate_tokens(t)
        return Embedded(vectors, tokens)


class LiteLLMEmbedder:
    def __init__(self, spec: str, *, dimensions: int) -> None:
        self._spec, self._dims = spec, dimensions

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def native(self) -> bool:
        return False

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded:
        litellm = import_litellm()
        response = await litellm.aembedding(
            model=self._spec,
            input=list(texts),
            dimensions=self._dims,
            drop_params=True,  # a provider without `dimensions` is caught by _checked below
            num_retries=LLM_RETRIES,
        )
        rows = sorted(response.data, key=lambda d: d["index"])
        vectors = [normalize([float(x) for x in d["embedding"]]) for d in rows]
        _checked(vectors, texts, self._spec, self._dims)
        usage = getattr(response, "usage", None)
        reported = getattr(usage, "prompt_tokens", None) if usage is not None else None
        tokens = int(reported) if reported else sum(estimate_tokens(t) for t in texts)
        return Embedded(vectors, tokens)


def hash_vector(text: str, dimensions: int) -> list[float]:
    """Feature hashing of lower-cased words: texts sharing words get similar vectors."""
    vector = [0.0] * dimensions
    for word in _WORD.findall(text.lower()):
        digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        vector[bucket] += 1.0 if digest[4] & 1 else -1.0
    return normalize(vector)


class HashEmbedder:
    """Deterministic and offline (spec §13): tests and `augury eval retrieval --fake-embedder`
    use it to check the code paths, never the quality. It spends nothing."""

    def __init__(self, dimensions: int = 64, spec: str = "hash/feature-hashing") -> None:
        self._spec, self._dims = spec, dimensions
        self.calls: list[tuple[EmbedTask, int]] = []  # (task, texts) per call, for tests

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def native(self) -> bool:
        return False

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded:
        self.calls.append((task, len(texts)))
        return Embedded([hash_vector(t, self._dims) for t in texts], tokens=0)


def _genai_retries() -> types.HttpOptions:
    retry = types.HttpRetryOptions(
        attempts=LLM_RETRIES, initial_delay=1.0, http_status_codes=RETRY_STATUSES
    )
    return types.HttpOptions(retry_options=retry)


def resolve_embedder(config: Config, env: Mapping[str, str] | None = None) -> Embedder:
    """Builds the embedder only; nothing is sent until it embeds. Raises EmbedderUnavailable."""
    spec, dims = config.embeddings.model, config.embeddings.dimensions
    if not spec:
        raise EmbedderUnavailable("embeddings are off ([embeddings] model is empty)")
    env = os.environ if env is None else env
    provider = spec.partition("/")[0]
    if provider == "gemini":
        key = next((env[var] for var in GEMINI_KEY_VARS if env.get(var)), None)
        if key is None:
            raise EmbedderUnavailable(f"{spec} needs GEMINI_API_KEY (or GOOGLE_API_KEY)")
        client = genai.Client(api_key=key, http_options=_genai_retries())
        return GenAIEmbedder(spec, client, dimensions=dims, one_per_call=False)
    if provider == "vertex_ai":
        project = config.google.project or env.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise EmbedderUnavailable(f"{spec} needs [google] project (or GOOGLE_CLOUD_PROJECT)")
        location = config.google.location or env.get("GOOGLE_CLOUD_LOCATION", "") or "global"
        client = genai.Client(
            vertexai=True, project=project, location=location, http_options=_genai_retries()
        )
        return GenAIEmbedder(spec, client, dimensions=dims, one_per_call=True)
    try:
        litellm = import_litellm()
    except ImportError:
        raise EmbedderUnavailable(f"{spec} goes through LiteLLM: {PROVIDERS_HINT}") from None
    check = litellm.validate_environment(model=spec)
    missing = check.get("missing_keys") or []
    if missing and not check.get("keys_in_environment"):
        raise EmbedderUnavailable(f"{spec} needs {', '.join(missing)} in your environment or .env")
    return LiteLLMEmbedder(spec, dimensions=dims)


def index_problem(index: Sequence[tuple[str, int]], embedder: Embedder) -> str | None:
    """The mixed-model guard (spec §6.3): None when every stored vector came from this
    embedder at this size (or none is stored yet), else what to do about it."""
    current = (embedder.spec, embedder.dimensions)
    if all(entry == current for entry in index):
        return None
    built = ", ".join(f"{model} ({dims} dims)" for model, dims in sorted(set(index)))
    return (
        f"the search index holds vectors from {built}, but [embeddings] is {embedder.spec}"
        f" ({embedder.dimensions} dims): run `augury reindex`"
    )


@dataclass
class EmbedMeter:
    """Budget and usage for embedding calls, on the run that asked for them."""

    conn: sqlite3.Connection
    config: Config
    run_id: str
    now: Callable[[], datetime]

    async def embed(
        self, embedder: Embedder, texts: Sequence[str], task: EmbedTask
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        runs = RunsRepo(self.conn)
        for batch in batched(texts, EMBED_BATCH, strict=False):
            spend = today_spend(self.conn, self.now())
            if (problem := budget_problem(spend, self.config.budget)) is not None:
                raise BudgetExceeded(problem)  # checked before the call: nothing is spent
            result = await embedder.embed(batch, task)
            vectors += result.vectors
            if result.tokens == 0:
                continue
            price = price_for(embedder.spec, self.config, native=embedder.native)
            runs.add_usage(
                self.run_id,
                tokens_in=result.tokens,
                tokens_out=0,
                cost_usd=price.cost(result.tokens, 0) if price else 0.0,
                unpriced_tokens=0 if price else result.tokens,
            )
        return vectors
