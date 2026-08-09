"""A triage decision: category, confidence, reasoning and which tier fired."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import DecidedBy, DecisionStatus, ProposedAction


class Decision(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    item_id: str
    run_id: str
    cluster_id: str | None = None
    category_id: str | None = None

    proposed_action: ProposedAction = ProposedAction.KEEP
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Full human-readable justification, expandable in the UI.
    reasoning: str = ""
    #: The which-tier-fired indicator surfaced as a badge in the queue.
    decided_by: DecidedBy
    rule_id: str | None = None
    time_sensitive: bool = False
    status: DecisionStatus = DecisionStatus.PROPOSED
    decided_at: datetime | None = None

    def below_floor(self, confidence_floor: float) -> bool:
        """True when this decision is too uncertain to ever archive automatically."""
        return self.confidence < confidence_floor

    def enforce_floor(self, confidence_floor: float) -> "Decision":
        """Never-miss guard: an archive below the floor becomes ``needs_your_call``.

        Errs toward keeping mail visible, never toward hiding it.
        """
        if (
            self.proposed_action is ProposedAction.ARCHIVE
            and self.below_floor(confidence_floor)
        ):
            return self.model_copy(
                update={
                    "proposed_action": ProposedAction.KEEP,
                    "status": DecisionStatus.NEEDS_YOUR_CALL,
                }
            )
        return self


__all__ = ["Decision"]
