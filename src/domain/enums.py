"""String enums shared by the DB layer, the graph, and the API.

Values are stored as plain strings in SQLite/Postgres columns (never DB-native
enums — Postgres-compatible and migration-friendly per spec/data.md).
"""

from enum import StrEnum


class GmailAccountStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"


class CategoryRule(StrEnum):
    LABEL_ONLY = "label_only"
    LABEL_AND_ARCHIVE = "label_and_archive"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    UNDONE = "undone"


class RunTrigger(StrEnum):
    CLEAN_CHUNK = "clean_chunk"


class MutationAction(StrEnum):
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    REMOVE_INBOX = "remove_inbox"
    RESTORE_INBOX = "restore_inbox"


#: Undo inverse pairs (spec/data.md): add_label↔remove_label, remove_inbox↔restore_inbox.
MUTATION_INVERSE: dict[MutationAction, MutationAction] = {
    MutationAction.ADD_LABEL: MutationAction.REMOVE_LABEL,
    MutationAction.REMOVE_LABEL: MutationAction.ADD_LABEL,
    MutationAction.REMOVE_INBOX: MutationAction.RESTORE_INBOX,
    MutationAction.RESTORE_INBOX: MutationAction.REMOVE_INBOX,
}


class RunEventType(StrEnum):
    CHUNK_LOADED = "chunk_loaded"
    DECISION = "decision"
    ACTION = "action"
    FALLBACK = "fallback"
    COST_TICK = "cost_tick"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_FINISHED = "run_finished"
    UNDO_STARTED = "undo_started"
    UNDO_ACTION = "undo_action"
    UNDO_FINISHED = "undo_finished"


class DecisionSource(StrEnum):
    LLM = "llm"
    PROFILE = "profile"


class LlmProvider(StrEnum):
    NVIDIA = "nvidia"
    GEMINI = "gemini"


class ProfileOrigin(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
