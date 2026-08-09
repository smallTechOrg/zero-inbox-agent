"""Closed vocabularies shared by the domain models and the persistence layer."""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    GMAIL = "gmail"


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    REAUTH_REQUIRED = "reauth_required"
    REVOKED = "revoked"


class ProposedAction(StrEnum):
    """The complete set of actions. There is deliberately no delete/trash/spam."""

    KEEP = "keep"
    ARCHIVE = "archive"
    DIGEST = "digest"


class DecidedBy(StrEnum):
    """Which tier of the cost-tiered cascade produced the decision."""

    RULE = "rule"
    SENDER_HISTORY = "sender_history"
    LLM = "llm"
    LLM_DEEP = "llm_deep"
    REVIEWER = "reviewer"
    ERROR = "error"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    UNDONE = "undone"
    NEEDS_YOUR_CALL = "needs_your_call"


class ClusterKind(StrEnum):
    LIST = "list"
    SENDER = "sender"
    DOMAIN = "domain"
    CATEGORY = "category"


class RunKind(StrEnum):
    INCREMENTAL = "incremental"
    BACKLOG = "backlog"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuleKind(StrEnum):
    DETERMINISTIC = "deterministic"
    LEARNED = "learned"
    LLM_PROPOSED = "llm_proposed"


class RuleSource(StrEnum):
    SEED_PACK = "seed_pack"
    MINED = "mined"
    CHAT = "chat"
    USER = "user"


class RuleStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    #: The only state in which the agent acts without per-decision approval.
    AUTOMATIC = "automatic"
    DISABLED = "disabled"


__all__ = [
    "Channel",
    "ClusterKind",
    "ConnectionStatus",
    "DecidedBy",
    "DecisionStatus",
    "ProposedAction",
    "RuleKind",
    "RuleSource",
    "RuleStatus",
    "RunKind",
    "RunStatus",
]
