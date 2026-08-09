"""A group of decisions presented as one sweepable unit in the triage queue."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import ClusterKind, ProposedAction


class Cluster(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    run_id: str
    kind: ClusterKind = ClusterKind.SENDER
    label: str = ""
    item_count: int = 0
    suggested_action: ProposedAction = ProposedAction.KEEP
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Up to three subjects shown as a preview; not persisted on the cluster row.
    sample_subjects: list[str] = Field(default_factory=list)


__all__ = ["Cluster"]
