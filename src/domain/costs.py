"""CostTotals — mutable per-run cost accumulator (spec/agent.md RunState.cost)."""

from dataclasses import dataclass


@dataclass(slots=True)
class CostTotals:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0
    fallback_events: int = 0

    def add_call(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        est_cost_usd: float,
        was_fallback: bool = False,
    ) -> None:
        self.llm_calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.est_cost_usd += est_cost_usd
        if was_fallback:
            self.fallback_events += 1
