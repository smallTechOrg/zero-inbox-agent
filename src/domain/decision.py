"""Decision — one classification outcome for one thread (spec/agent.md)."""

from dataclasses import dataclass

from domain.enums import DecisionSource

#: Below this confidence the decision keeps its best-guess category AND is
#: flagged needs_review (spec/agent.md classify_batch).
NEEDS_REVIEW_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class Decision:
    gmail_thread_id: str
    category_id: str
    confidence: float                 # 0–1
    reason: str                       # one line, plain English
    needs_review: bool
    source: DecisionSource = DecisionSource.LLM
