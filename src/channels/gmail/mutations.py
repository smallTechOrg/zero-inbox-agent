"""The ONE choke point for every Gmail write this system performs.

spec/architecture.md is the source of truth. Exactly four reversible mutations
exist, and nothing else is representable:

=================  ============================================  ==============
op                 Gmail effect                                  inverse
=================  ============================================  ==============
``add_label``      add one label to a thread                     ``remove_label``
``remove_label``   remove one label from a thread                ``add_label``
``remove_inbox``   remove the ``INBOX`` label (== archive)       ``restore_inbox``
``restore_inbox``  re-add the ``INBOX`` label                    ``remove_inbox``
=================  ============================================  ==============

Guarantees enforced *here*, not by callers:

1. **Audit row first.** ``audit_writer`` (when provided) is invoked with the
   mutation record *before* the Gmail API is touched. A crash between the two
   leaves an audit row for a mutation that may not have happened — which undo
   handles idempotently — never a mutation without an audit row.
2. **Closed op set.** Any op outside :data:`ALLOWED_MUTATIONS` raises
   :class:`ForbiddenMutation`. There is no delete/trash/spam surface anywhere.
3. **Test-isolation guard.** When ``AGENT_GMAIL_WRITE_DISABLED=1`` (set by the
   test suite / e2e fixtures) the mutation is recorded and returned as applied
   without calling Gmail's write API. Read paths stay real. Additionally,
   ``tests/conftest.py`` wraps :meth:`GmailMutator._execute` so a real
   ``googleapiclient`` request can never be executed from inside a test.

Transport: 3x retry with backoff, 401/403/invalid-grant → :class:`ReauthRequired`
(the caller maps it to the structured ``gmail_reconnect`` error — never a
traceback), 429/5xx → backoff then :class:`RateLimited`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from channels.base import ChannelError, RateLimited, ReauthRequired

INBOX_LABEL_ID = "INBOX"
MAX_ATTEMPTS = 3

MUTATION_ADD_LABEL = "add_label"
MUTATION_REMOVE_LABEL = "remove_label"
MUTATION_REMOVE_INBOX = "remove_inbox"
MUTATION_RESTORE_INBOX = "restore_inbox"

#: The exactly-four reversible mutations. This tuple IS the whitelist.
ALLOWED_MUTATIONS: tuple[str, ...] = (
    MUTATION_ADD_LABEL,
    MUTATION_REMOVE_LABEL,
    MUTATION_REMOVE_INBOX,
    MUTATION_RESTORE_INBOX,
)

#: Undo applies the inverse of each audit row, newest first (spec/data.md).
INVERSE_MUTATION: dict[str, str] = {
    MUTATION_ADD_LABEL: MUTATION_REMOVE_LABEL,
    MUTATION_REMOVE_LABEL: MUTATION_ADD_LABEL,
    MUTATION_REMOVE_INBOX: MUTATION_RESTORE_INBOX,
    MUTATION_RESTORE_INBOX: MUTATION_REMOVE_INBOX,
}

#: The env flags honoured by the test-isolation guard (see module docstring).
#: Either one being "1" disables live Gmail writes.
WRITE_DISABLED_ENV = "AGENT_GMAIL_WRITE_DISABLED"
TEST_ISOLATION_ENV = "AGENT_TEST_ISOLATION"


class ForbiddenMutation(ChannelError):
    """An op outside the four-op whitelist was requested. Always a bug."""


def gmail_writes_disabled() -> bool:
    """True when the test-isolation guard has switched Gmail writes off."""
    return (
        os.environ.get(WRITE_DISABLED_ENV, "") == "1"
        or os.environ.get(TEST_ISOLATION_ENV, "") == "1"
    )


class GmailMutator:
    """Applies exactly one of the four whitelisted mutations to one thread.

    ``audit_writer`` — optional callable invoked with the mutation record
    *before* Gmail is called::

        {"op", "gmail_thread_id", "label_id", "reason"}

    The runs/undo slice passes a writer that persists the audit row; the
    mini-audit and all read paths never construct a mutator at all.
    """

    def __init__(
        self,
        service,
        *,
        audit_writer: Callable[[dict[str, Any]], None] | None = None,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._audit_writer = audit_writer
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    # --- transport ------------------------------------------------------
    def _execute(self, request):
        last: HttpError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute()
            except RefreshError as exc:
                # A revoked/expired refresh token fails when google-auth mints
                # the access token — never as an HttpError. Unmapped it escapes
                # as a raw traceback; the user must see "reconnect Gmail".
                raise ReauthRequired(
                    "Gmail rejected the stored credentials — reconnect Gmail"
                ) from exc
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status in (401, 403):
                    raise ReauthRequired(
                        "Gmail rejected the stored credentials — reconnect Gmail"
                    ) from exc
                if status in (429, 500, 502, 503, 504):
                    last = exc
                    self._sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise ChannelError(f"Gmail request failed with HTTP {status}") from exc
        raise RateLimited("Gmail is rate limiting; retries exhausted") from last

    # --- reads used by the undo machinery -------------------------------
    def get_thread_labels(self, thread_id: str) -> list[str]:
        """Current Gmail label IDs for a thread (pre-mutation snapshot)."""
        result = self._execute(
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
        )
        return list((result or {}).get("labelIds") or [])

    # --- THE choke point -------------------------------------------------
    def apply(
        self,
        op: str,
        thread_id: str,
        *,
        label_id: str | None = None,
        reason: str = "",
    ) -> dict:
        """Apply one whitelisted mutation. Everything above calls this and only this.

        Returns ``{"thread_id", "op", "label_id", "applied", "simulated"}`` —
        ``simulated`` is True only under the test-isolation guard.
        """
        if op not in ALLOWED_MUTATIONS:
            raise ForbiddenMutation(
                f"mutation {op!r} is not one of {ALLOWED_MUTATIONS} — "
                "no other Gmail write exists in this system"
            )
        if op in (MUTATION_ADD_LABEL, MUTATION_REMOVE_LABEL) and not label_id:
            raise ForbiddenMutation(f"mutation {op!r} requires a label_id")
        if op in (MUTATION_REMOVE_INBOX, MUTATION_RESTORE_INBOX):
            label_id = INBOX_LABEL_ID
        if not thread_id:
            raise ForbiddenMutation("mutation requires a gmail thread id")

        record = {
            "op": op,
            "gmail_thread_id": thread_id,
            "label_id": label_id,
            "reason": reason,
        }
        # (1) Audit row FIRST — before Gmail is touched.
        if self._audit_writer is not None:
            self._audit_writer(dict(record))

        # (3) Test-isolation guard: recorded as applied, Gmail write API untouched.
        if gmail_writes_disabled():
            return {**record, "applied": True, "simulated": True}

        add = [label_id] if op in (MUTATION_ADD_LABEL, MUTATION_RESTORE_INBOX) else []
        remove = [label_id] if op in (MUTATION_REMOVE_LABEL, MUTATION_REMOVE_INBOX) else []
        result = self._execute(
            self._service.users()
            .threads()
            .modify(
                userId="me",
                id=thread_id,
                body={"addLabelIds": add, "removeLabelIds": remove},
            )
        )
        return {
            **record,
            "applied": True,
            "simulated": False,
            "label_ids": list((result or {}).get("labelIds") or []),
        }

    def apply_inverse(self, op: str, thread_id: str, *, label_id: str | None = None, reason: str = "") -> dict:
        """Apply the inverse of ``op`` — the undo primitive."""
        if op not in INVERSE_MUTATION:
            raise ForbiddenMutation(f"mutation {op!r} has no inverse — not a whitelisted op")
        return self.apply(INVERSE_MUTATION[op], thread_id, label_id=label_id, reason=reason)


def execute_mutation(
    db,
    *,
    user_id: str,
    run_id: str,
    gmail_thread_id: str,
    action: str,
    label_name: str | None = None,
    reason: str = "",
) -> dict:
    """The triage-graph entry point (graph/runner contract, spec/roadmap.md).

    Validates the op, writes the ``mutations`` audit row FIRST on the caller's
    session, then applies the Gmail write through :class:`GmailMutator` (which
    also enforces the test-isolation guard). ``label_name`` is ensured as a
    real Gmail label lazily on the live path only.
    """
    if action not in ALLOWED_MUTATIONS:
        raise ForbiddenMutation(
            f"mutation {action!r} is not one of {ALLOWED_MUTATIONS} — "
            "no other Gmail write exists in this system"
        )
    if action in (MUTATION_ADD_LABEL, MUTATION_REMOVE_LABEL) and not label_name:
        raise ForbiddenMutation(f"mutation {action!r} requires a label_name")
    if not gmail_thread_id:
        raise ForbiddenMutation("mutation requires a gmail thread id")

    from db.models import Mutation

    # (1) Audit row FIRST — before Gmail is touched.
    db.add(
        Mutation(
            user_id=user_id,
            run_id=run_id,
            gmail_thread_id=gmail_thread_id,
            action=action,
            label_name=label_name,
            reason=reason or "",
        )
    )
    db.flush()

    record = {
        "op": action,
        "gmail_thread_id": gmail_thread_id,
        "label_id": None,
        "reason": reason or "",
    }
    # (2) Test-isolation guard: recorded as applied, Gmail write API untouched.
    if gmail_writes_disabled():
        return {**record, "applied": True, "simulated": True}

    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.store import adapter_for_user

    adapter = adapter_for_user(user_id=user_id)
    service = adapter._service  # noqa: SLF001 — serial single-caller use
    label_id: str | None = None
    if action in (MUTATION_ADD_LABEL, MUTATION_REMOVE_LABEL):
        label_id = GmailLabelManager(service).ensure_label(label_name)["id"]
        if label_id:
            # Write the lazily-created label id back onto the category so the
            # undo path can invert add_label rows (api/runs.label_lookup).
            from db.models import Category

            bare = label_name.split("/", 1)[1] if "/" in label_name else label_name
            db.query(Category).filter(
                Category.user_id == user_id,
                Category.name == bare,
                Category.gmail_label_id.is_(None),
            ).update({"gmail_label_id": label_id}, synchronize_session=False)
    return GmailMutator(service).apply(
        action, gmail_thread_id, label_id=label_id, reason=reason or ""
    )
