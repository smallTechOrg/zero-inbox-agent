"""The result of one triage run — what the graph hands back to the API layer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from domain.cluster import Cluster
from domain.decision import Decision
from domain.enums import DecidedBy, DecisionStatus, RunStatus


class TriageOutcome(BaseModel):
    """Aggregate view of a run: decisions, clusters and tier/cost accounting."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    status: RunStatus = RunStatus.RUNNING
    dry_run: bool = True
    items_total: int = 0
    decisions: list[Decision] = Field(default_factory=list)
    clusters: list[Cluster] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error_message: str | None = None

    @property
    def items_decided(self) -> int:
        return len(self.decisions)

    def counts_by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            counts[str(d.decided_by)] = counts.get(str(d.decided_by), 0) + 1
        return counts

    def counts_by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            key = d.category_id or "uncategorized"
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def needs_your_call(self) -> int:
        return sum(1 for d in self.decisions if d.status is DecisionStatus.NEEDS_YOUR_CALL)

    @property
    def rules_vs_llm_ratio(self) -> float:
        """Share of decisions resolved without an LLM call (0.0–1.0)."""
        if not self.decisions:
            return 0.0
        cheap = {DecidedBy.RULE, DecidedBy.SENDER_HISTORY}
        return sum(1 for d in self.decisions if d.decided_by in cheap) / len(self.decisions)

    def counts(self) -> dict:
        """The ``triage_runs.counts`` JSON payload."""
        return {
            "by_tier": self.counts_by_tier(),
            "by_category": self.counts_by_category(),
            "needs_your_call": self.needs_your_call,
        }


__all__ = ["TriageOutcome"]
