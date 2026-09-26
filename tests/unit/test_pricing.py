import sys
from types import ModuleType

import pytest

from augury.core.config import Config, PriceConfig
from augury.llm.pricing import Price, packaged_prices, price_for


def _fake_litellm(cost_per_token) -> ModuleType:
    module = ModuleType("litellm")
    module.__dict__.update(cost_per_token=cost_per_token, telemetry=True)
    return module


def test_cost_is_per_million_tokens():
    assert Price(0.30, 2.50).cost(1_000_000, 100_000) == pytest.approx(0.55)


def test_the_default_models_have_packaged_prices():
    config = Config()
    assert {config.models.fast, config.models.smart} <= set(packaged_prices())


def test_a_config_override_beats_the_packaged_price():
    spec = Config().models.fast
    config = Config(pricing={spec: PriceConfig(input_per_mtok=9, output_per_mtok=9)})
    assert price_for(spec, config, native=True) == Price(9, 9)


def test_an_unknown_native_model_has_no_price():
    assert price_for("gemini/not-a-real-model", Config(), native=True) is None


def test_litellm_models_use_litellms_cost_map(monkeypatch):
    fake = _fake_litellm(lambda **_: (0.001, 0.004))  # USD for 1,000 tokens each way
    monkeypatch.setitem(sys.modules, "litellm", fake)
    price = price_for("openai/some-model", Config(), native=False)
    assert price is not None
    assert (price.input_per_mtok, price.output_per_mtok) == (pytest.approx(1.0), pytest.approx(4.0))
    assert fake.__dict__["telemetry"] is False


def test_a_model_litellm_does_not_know_has_no_price(monkeypatch):
    def unknown(**_):
        raise ValueError("This model isn't mapped yet")

    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(unknown))
    assert price_for("openai/mystery", Config(), native=False) is None
