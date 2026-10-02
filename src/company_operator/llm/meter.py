from __future__ import annotations

from collections import defaultdict

from company_operator.config import ModelPrice
from company_operator.llm.client import Usage


class CostMeter:
    """Accumulates token usage and estimates cost in USD for one Run."""

    def __init__(self, prices: dict[str, ModelPrice]) -> None:
        self._prices = prices
        self.total_usd = 0.0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cached_tokens = 0
        self.by_model: dict[str, float] = defaultdict(float)
        self.unknown_models: set[str] = set()

    def add(self, model: str, usage: Usage) -> float:
        price = self._prices.get(model)
        if price is None:
            self.unknown_models.add(model)
            price = ModelPrice(input=0.0, cached_input=0.0, output=0.0)
        uncached = max(usage.prompt_tokens - usage.cached_tokens, 0)
        cost = (
            uncached * price.input
            + usage.cached_tokens * price.cached_input
            + usage.completion_tokens * price.output
        ) / 1_000_000
        self.total_usd += cost
        self.by_model[model] += cost
        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        self.total_cached_tokens += usage.cached_tokens
        return cost

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    def snapshot(self) -> dict[str, object]:
        return {
            "usd": round(self.total_usd, 6),
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "cached_tokens": self.total_cached_tokens,
            "by_model": {model: round(cost, 6) for model, cost in self.by_model.items()},
            "unknown_models": sorted(self.unknown_models),
        }
