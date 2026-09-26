import sys
import tomllib
from importlib.resources import files

import pytest
from google.adk.models.google_llm import Gemini
from pydantic import ValidationError

from augury.core.config import Config, ConfigError, GoogleConfig, ModelsConfig, load_config
from augury.llm.resolver import (
    ModelUnavailable,
    default_resolver,
    model_spec,
    resolve_model,
    role_statuses,
)
from tests.helpers import ScriptedLlm, fake_resolver

KEY = {"GEMINI_API_KEY": "test-key"}


def test_default_models_come_from_the_packaged_defaults():
    text = files("augury.llm").joinpath("defaults.toml").read_text(encoding="utf-8")
    defaults = tomllib.loads(text)["models"]
    config = Config()
    assert (config.models.fast, config.models.smart) == (defaults["fast"], defaults["smart"])
    assert config.models.fast.startswith("gemini/") and config.models.smart.startswith("gemini/")


def test_gemini_without_a_key_is_unavailable():
    with pytest.raises(ModelUnavailable, match="GEMINI_API_KEY"):
        resolve_model(Config(), "fast", env={})


def test_gemini_resolves_to_adks_native_class_with_retries():
    resolved = resolve_model(Config(), "fast", "triage", env=KEY)
    llm = resolved.llm
    assert isinstance(llm, Gemini) and resolved.native and resolved.provider == "gemini"
    assert llm.model == Config().models.fast.removeprefix("gemini/")
    assert llm.client_kwargs == {"api_key": "test-key"}
    assert llm.retry_options is not None and llm.retry_options.attempts == 3
    assert 429 in (llm.retry_options.http_status_codes or [])


def test_google_api_key_wins_like_it_does_in_google_genai():
    llm = resolve_model(Config(), "fast", env={"GOOGLE_API_KEY": "g", "GEMINI_API_KEY": "m"}).llm
    assert isinstance(llm, Gemini) and llm.client_kwargs == {"api_key": "g"}


def test_an_agent_override_beats_its_role():
    config = Config(models=ModelsConfig(overrides={"triage": "gemini/gemini-custom"}))
    assert model_spec(config, "fast", "triage") == "gemini/gemini-custom"
    assert model_spec(config, "fast", "summarizer") == config.models.fast
    assert resolve_model(config, "fast", "triage", env=KEY).spec == "gemini/gemini-custom"


def test_vertex_ai_uses_the_native_class_with_project_and_location():
    config = Config(
        models=ModelsConfig(fast="vertex_ai/gemini-x"), google=GoogleConfig(project="p1")
    )
    resolved = resolve_model(config, "fast", env={})
    llm = resolved.llm
    assert isinstance(llm, Gemini) and resolved.native and llm.model == "gemini-x"
    assert llm.client_kwargs == {"vertexai": True, "project": "p1", "location": "global"}


def test_vertex_ai_without_a_project_is_unavailable():
    config = Config(models=ModelsConfig(fast="vertex_ai/gemini-x"))
    with pytest.raises(ModelUnavailable, match=r"\[google\] project"):
        resolve_model(config, "fast", env={})


def test_other_providers_need_the_providers_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.adk.models.lite_llm", None)  # litellm is absent
    config = Config(models=ModelsConfig(fast="openai/some-model"))
    with pytest.raises(ModelUnavailable, match=r"augury\[providers\]"):
        resolve_model(config, "fast", env={})


def test_litellm_models_resolve_when_the_extra_is_installed(monkeypatch):
    pytest.importorskip("litellm")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    resolved = resolve_model(Config(models=ModelsConfig(fast="openai/some-model")), "fast")
    assert not resolved.native and resolved.provider == "openai"
    assert resolved.llm.model == "openai/some-model"


def test_model_strings_must_name_a_provider(paths):
    with pytest.raises(ValidationError):
        ModelsConfig(fast="gemini-without-a-provider")
    paths.config_file.write_text('[models]\nfast = "no-slash"\n')
    with pytest.raises(ConfigError, match=r"models\.fast"):
        load_config(paths)


def test_overrides_must_name_a_known_agent():
    with pytest.raises(ValidationError, match="unknown agent"):
        ModelsConfig(overrides={"writer": "gemini/x"})


def test_role_statuses_report_each_role():
    missing = role_statuses(Config(), default_resolver(Config(), env={}))
    assert [(s.role, s.ok) for s in missing] == [("fast", False), ("smart", False)]
    assert "GEMINI_API_KEY" in missing[0].detail and missing[0].native
    ready = role_statuses(Config(), fake_resolver(ScriptedLlm()))
    assert all(s.ok and s.provider == "fake" for s in ready)
