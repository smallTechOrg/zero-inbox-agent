"""Graph state and value types for the triage run.

The privacy boundary (spec/architecture.md) is enforced by construction: the
canonical :class:`llm.views.ClassifierView` — re-exported here — is the ONLY
shape that ever reaches an LLM prompt, and an email body is unrepresentable in
it. :func:`view_from_mapping` is the graph's tolerant constructor for whatever
the Gmail layer hands over: every key outside the allowed prompt fields is
dropped on the floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping, TypedDict

from llm.views import ALLOWED_PROMPT_FIELDS, ClassifierView

__all__ = [
    "ALLOWED_PROMPT_FIELDS",
    "CONFIDENCE_REVIEW_THRESHOLD",
    "INVALID_OUTPUT_REASON",
    "LABEL_NAMESPACE",
    "ClassifierView",
    "ClassifyOutcome",
    "CostTotals",
    "Decision",
    "RunState",
    "TaxonomyEntry",
    "label_for",
    "view_from_mapping",
]

#: Confidence below this ⇒ keep the best-guess category AND flag for review.
CONFIDENCE_REVIEW_THRESHOLD = 0.7

#: All agent-created Gmail labels are namespaced so they are fully removable.
LABEL_NAMESPACE = "ZI"

#: Reason recorded when the classifier returned nothing usable for a thread.
INVALID_OUTPUT_REASON = "classifier output invalid"


def label_for(category_name: str) -> str:
    """The Gmail label an agent category materialises as: ``ZI/<Category>``."""
    return f"{LABEL_NAMESPACE}/{category_name}"


def view_from_mapping(data: Mapping[str, Any]) -> ClassifierView:
    """Build a ClassifierView from any mapping, dropping every disallowed key.

    Even a leaky upstream that includes ``body`` cannot get it into a prompt:
    the field does not survive this constructor and is unrepresentable in the
    frozen, slotted ClassifierView type.
    """
    allowed = {k: v for k, v in data.items() if k in ALLOWED_PROMPT_FIELDS}
    allowed.setdefault("sender_address", "")
    return ClassifierView(**allowed)


@dataclass(frozen=True, slots=True)
class TaxonomyEntry:
    """One user category as the graph needs it (loaded fresh each run)."""

    id: str
    name: str
    description: str = ""
    rule: str = "label_only"  # label_only | label_and_archive
    is_needs_review: bool = False


@dataclass(slots=True)
class Decision:
    """One per-thread decision (spec/agent.md)."""

    thread_id: str
    category_id: str
    category_name: str
    confidence: float
    reason: str
    needs_review: bool
    # Carried for feed sentences and the thread_decisions row; header data only.
    subject: str = ""
    sender: str = ""


@dataclass(slots=True)
class CostTotals:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0
    fallback_events: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ClassifyOutcome:
    """What one batched ``classify_batch()`` LLM call produced.

    Adapter over :class:`llm.BatchClassification`: results are strict
    ``{thread_id, category, confidence, reason}`` dicts; anything missing or
    malformed is reported, never guessed. Provider/fallback metadata powers the
    ``fallback`` feed event and the cost ticker.
    """

    results: list[dict[str, Any]] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    provider: str = "nvidia"
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0
    latency_ms: int = 0
    was_fallback: bool = False
    fallback_reason: str = ""


class RunState(TypedDict, total=False):
    run_id: str
    user_id: str
    chunk_limit: int
    threads: list[ClassifierView]
    batches: list[list[ClassifierView]]
    batch_index: int
    decisions: list[Decision]
    applied_count: int  # decisions[:applied_count] have had their actions applied
    counts: dict[str, int]
    cost: CostTotals
    error: str | None
