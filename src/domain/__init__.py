"""Domain types shared across the backend — enums, the privacy-bounded
ClassifierView, Decision, and cost accounting. No SQLAlchemy, no FastAPI."""

from domain.classifier_view import CLASSIFIER_VIEW_FIELDS, SNIPPET_MAX_CHARS, ClassifierView
from domain.costs import CostTotals
from domain.decision import NEEDS_REVIEW_CONFIDENCE_THRESHOLD, Decision
from domain.enums import (
    MUTATION_INVERSE,
    CategoryRule,
    DecisionSource,
    GmailAccountStatus,
    LlmProvider,
    MutationAction,
    ProfileOrigin,
    RunEventType,
    RunStatus,
    RunTrigger,
)

__all__ = [
    "CLASSIFIER_VIEW_FIELDS",
    "MUTATION_INVERSE",
    "NEEDS_REVIEW_CONFIDENCE_THRESHOLD",
    "SNIPPET_MAX_CHARS",
    "CategoryRule",
    "ClassifierView",
    "CostTotals",
    "Decision",
    "DecisionSource",
    "GmailAccountStatus",
    "LlmProvider",
    "MutationAction",
    "ProfileOrigin",
    "RunEventType",
    "RunStatus",
    "RunTrigger",
]
