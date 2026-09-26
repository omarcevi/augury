"""Role → ADK model (spec §5.1). `gemini/…` and `vertex_ai/gemini…` use ADK's native Gemini
class; every other provider goes through LiteLLM, which the optional `providers` extra installs."""

import importlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from augury.core.config import Config

Role = Literal["fast", "smart"]
ROLES: tuple[Role, ...] = ("fast", "smart")
# google-genai reads GOOGLE_API_KEY first when both are set (google/genai/_api_client.py).
GEMINI_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
LLM_RETRIES = 3  # spec §11: rate limits and 5xx are retried with backoff, at most 3 times
RETRY_STATUSES = [429, 500, 502, 503, 504]
PROVIDERS_HINT = "uv tool install 'augury[providers]'"
NATIVE_PROVIDERS = ("gemini", "vertex_ai")


class ModelUnavailable(Exception):
    """A role can't be served right now. The message says why and what to do."""


@dataclass(frozen=True)
class ResolvedModel:
    spec: str  # as written in config, e.g. "gemini/<model-id>"
    provider: str  # "gemini", "vertex_ai", or a LiteLLM provider
    native: bool  # served by ADK's own Gemini class
    llm: BaseLlm


Resolver = Callable[[Role, str | None], ResolvedModel]


@dataclass(frozen=True)
class RoleStatus:
    role: Role
    spec: str
    provider: str
    native: bool
    ok: bool
    detail: str = ""
    degraded: bool = False  # from a doctor probe: answers, but can't call tools


def model_spec(config: Config, role: Role, agent: str | None = None) -> str:
    if agent is not None and agent in config.models.overrides:
        return config.models.overrides[agent]
    return config.models.fast if role == "fast" else config.models.smart


def _retry_options() -> types.HttpRetryOptions:
    return types.HttpRetryOptions(
        attempts=LLM_RETRIES, initial_delay=1.0, http_status_codes=RETRY_STATUSES
    )


def import_litellm() -> Any:
    """LiteLLM set up for privacy: no cost-map download at import (spec N5), no telemetry."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm: Any = importlib.import_module("litellm")
    litellm.telemetry = False
    return litellm


def _gemini(spec: str, name: str, env: Mapping[str, str]) -> ResolvedModel:
    key = next((env[var] for var in GEMINI_KEY_VARS if env.get(var)), None)
    if key is None:
        raise ModelUnavailable(
            f"{spec} needs GEMINI_API_KEY (or GOOGLE_API_KEY): run `augury init`, "
            "or add it to the .env file in your config folder"
        )
    llm = Gemini(model=name, client_kwargs={"api_key": key}, retry_options=_retry_options())
    return ResolvedModel(spec, "gemini", True, llm)


def _vertex(spec: str, name: str, config: Config, env: Mapping[str, str]) -> ResolvedModel:
    project = config.google.project or env.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        raise ModelUnavailable(
            f"{spec} needs [google] project in config.toml (or GOOGLE_CLOUD_PROJECT)"
        )
    location = config.google.location or env.get("GOOGLE_CLOUD_LOCATION", "") or "global"
    llm = Gemini(
        model=name,
        client_kwargs={"vertexai": True, "project": project, "location": location},
        retry_options=_retry_options(),
    )
    return ResolvedModel(spec, "vertex_ai", True, llm)


def _litellm(spec: str, provider: str) -> ResolvedModel:
    try:
        wrapper: Any = importlib.import_module("google.adk.models.lite_llm")
        litellm = import_litellm()
    except ImportError:
        raise ModelUnavailable(f"{spec} goes through LiteLLM: {PROVIDERS_HINT}") from None
    check = litellm.validate_environment(model=spec)
    missing = check.get("missing_keys") or []
    if missing and not check.get("keys_in_environment"):
        raise ModelUnavailable(f"{spec} needs {', '.join(missing)} in your environment or .env")
    llm = wrapper.LiteLlm(model=spec, num_retries=LLM_RETRIES)
    return ResolvedModel(spec, provider, False, llm)


def resolve_model(
    config: Config,
    role: Role,
    agent: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ResolvedModel:
    """Builds the model object only; nothing is sent until an agent calls it."""
    spec = model_spec(config, role, agent)
    provider, _, name = spec.partition("/")
    env = os.environ if env is None else env
    if provider == "gemini":
        return _gemini(spec, name, env)
    if provider == "vertex_ai" and name.startswith("gemini"):
        return _vertex(spec, name, config, env)
    return _litellm(spec, provider)  # LiteLLM reads its keys from os.environ itself


def default_resolver(config: Config, env: Mapping[str, str] | None = None) -> Resolver:
    def resolve(role: Role, agent: str | None = None) -> ResolvedModel:
        return resolve_model(config, role, agent, env=env)

    return resolve


def role_statuses(config: Config, resolver: Resolver) -> tuple[RoleStatus, ...]:
    """Can each role be served? Offline: it builds the model objects and sends nothing."""
    statuses: list[RoleStatus] = []
    for role in ROLES:
        spec = model_spec(config, role)
        try:
            model = resolver(role, None)
        except ModelUnavailable as exc:
            provider = spec.partition("/")[0]
            native = provider in NATIVE_PROVIDERS
            statuses.append(RoleStatus(role, spec, provider, native, False, str(exc)))
        else:
            statuses.append(RoleStatus(role, model.spec, model.provider, model.native, True))
    return tuple(statuses)
