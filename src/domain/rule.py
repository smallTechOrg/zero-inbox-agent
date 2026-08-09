"""Deterministic / learned / proposed rules — tier 1 of the triage cascade."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import ProposedAction, RuleKind, RuleSource, RuleStatus


class RuleMatcher(BaseModel):
    """All present conditions must match (AND). An empty matcher matches nothing."""

    from_email: str | None = None
    from_domain: str | None = None
    list_id: str | None = None
    subject_regex: str | None = None
    has_attachment: bool | None = None
    older_than_days: int | None = None

    @property
    def is_empty(self) -> bool:
        return not any(v is not None for v in self.model_dump().values())


class RuleAction(BaseModel):
    set_category: str | None = None
    archive: bool = False
    digest: bool = False

    @property
    def proposed_action(self) -> ProposedAction:
        if self.archive:
            return ProposedAction.ARCHIVE
        if self.digest:
            return ProposedAction.DIGEST
        return ProposedAction.KEEP


class Rule(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    name: str
    kind: RuleKind = RuleKind.DETERMINISTIC
    source: RuleSource = RuleSource.SEED_PACK
    matcher: RuleMatcher = Field(default_factory=RuleMatcher)
    action: RuleAction = Field(default_factory=RuleAction)
    status: RuleStatus = RuleStatus.PROPOSED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_count: int = 0
    channel_filter_id: str | None = None
    promoted_at: datetime | None = None

    @property
    def acts_without_approval(self) -> bool:
        """Only ``automatic`` rules may act unattended."""
        return self.status is RuleStatus.AUTOMATIC


__all__ = ["Rule", "RuleAction", "RuleMatcher"]
