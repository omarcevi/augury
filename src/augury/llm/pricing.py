"""What a call costs (spec §5.9): a config override, then the packaged table, then (for models
routed through LiteLLM) LiteLLM's own cost map. None means the price is unknown."""

import tomllib
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from augury.core.config import Config
from augury.llm.resolver import import_litellm


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1_000_000


@cache
def packaged_prices() -> dict[str, Price]:
    text = files("augury.llm").joinpath("pricing.toml").read_text(encoding="utf-8")
    return {
        spec: Price(float(row["input_per_mtok"]), float(row["output_per_mtok"]))
        for spec, row in tomllib.loads(text).items()
    }


def _litellm_price(spec: str) -> Price | None:
    try:
        litellm = import_litellm()
        # 1,000 tokens, not a million: tiered models would quote their long-prompt rate.
        per_k_in, per_k_out = litellm.cost_per_token(
            model=spec, prompt_tokens=1000, completion_tokens=1000
        )
    except Exception:  # not installed, or LiteLLM doesn't know the model: the price is unknown
        return None
    return Price(float(per_k_in) * 1000, float(per_k_out) * 1000)


def price_for(spec: str, config: Config, *, native: bool) -> Price | None:
    if (override := config.pricing.get(spec)) is not None:
        return Price(override.input_per_mtok, override.output_per_mtok)
    if (packaged := packaged_prices().get(spec)) is not None:
        return packaged
    return None if native else _litellm_price(spec)
