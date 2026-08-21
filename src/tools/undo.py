"""Whole-run undo (spec/capabilities/run-undo-audit.md).

Replays the run's audit rows (``mutations``) **newest first**, applying the
inverse of each through the one Gmail write choke point
(:meth:`channels.gmail.mutations.GmailMutator.apply_inverse`), so after undo
every affected thread's labels and INBOX presence match its pre-run state
exactly.

Semantics:

* Granularity is the WHOLE RUN. Inverse map: add_label↔remove_label,
  remove_inbox↔restore_inbox — the only four ops that exist.
* **Resumable / idempotent**: each row's ``undone_at`` is committed the moment
  its inverse succeeds, and rows already stamped are never selected again. An
  interrupted undo, re-triggered, continues from the first non-undone row and
  double-inverts nothing.
* Undone threads become **re-decidable**: their ``thread_decisions`` rows get
  ``undone = true``, so a later run's never-redo index no longer blocks them.
* The undo is itself **audited and observable**: ``undo_started`` /
  ``undo_action`` / ``undo_finished`` (or ``undo_interrupted``) events are
  persisted to ``run_events`` and streamed on the run's SSE channel, and the
  ``undone_at`` stamps on the audit rows are the durable record.
* A revoked Google token surfaces as :class:`channels.base.ReauthRequired`
  (the API maps it to the structured ``gmail_reconnect`` error) — progress made
  before the failure stays committed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from channels.base import ReauthRequired
from db.models import Mutation, Run, ThreadDecision
from domain.enums import RunEventType, RunStatus
from events.store import record_run_event


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pending_mutations(session: Session, *, run_id: str, user_id: str) -> list[Mutation]:
    """This run's not-yet-undone audit rows, newest first (undo order)."""
    return list(
        session.execute(
            select(Mutation)
            .where(
                Mutation.user_id == user_id,
                Mutation.run_id == run_id,
                Mutation.undone_at.is_(None),
            )
            .order_by(Mutation.applied_at.desc(), Mutation.id.desc())
        )
        .scalars()
        .all()
    )


def undo_run(
    session: Session,
    *,
    run: Run,
    mutator,
    label_lookup: dict[str, str],
) -> dict:
    """Undo every remaining mutation of *run*. Returns a summary dict.

    ``mutator`` is a :class:`GmailMutator` for the run's user;
    ``label_lookup`` maps ``mutations.label_name`` → Gmail label id (built by
    the caller from the user's categories). Progress is committed row by row,
    so a crash or token failure mid-way loses nothing and never re-inverts.
    """
    user_id = run.user_id
    rows = pending_mutations(session, run_id=run.id, user_id=user_id)

    record_run_event(
        session,
        user_id=user_id,
        run_id=run.id,
        type=RunEventType.UNDO_STARTED,
        sentence=f"Undoing this run — restoring {len(rows)} Gmail changes.",
        detail={"pending": len(rows)},
    )
    session.commit()

    reversed_count = 0
    errors: list[str] = []

    for row in rows:
        label_id = None
        if row.action in ("add_label", "remove_label"):
            label_id = label_lookup.get(row.label_name or "")
            if not label_id:
                errors.append(
                    f"mutation {row.id}: no Gmail label id known for "
                    f"{row.label_name!r} — left as is"
                )
                continue
        try:
            mutator.apply_inverse(
                row.action,
                row.gmail_thread_id,
                label_id=label_id,
                reason=f"undo of run {run.id}",
            )
        except ReauthRequired:
            # Progress so far is already committed; surface reconnect upstream.
            session.commit()
            raise
        except Exception as exc:  # noqa: BLE001 — keep undoing the rest
            errors.append(f"mutation {row.id}: {exc}")
            continue

        row.undone_at = _utcnow()
        record_run_event(
            session,
            user_id=user_id,
            run_id=run.id,
            type=RunEventType.UNDO_ACTION,
            sentence=_undo_sentence(row),
            detail={"mutation_id": row.id, "action": row.action, "label": row.label_name},
        )
        # Durable per row: an interrupted undo resumes from the next row.
        session.commit()
        reversed_count += 1

    remaining = len(pending_mutations(session, run_id=run.id, user_id=user_id))
    if remaining == 0:
        # Undone threads are re-decidable by future runs.
        session.execute(
            update(ThreadDecision)
            .where(ThreadDecision.user_id == user_id, ThreadDecision.run_id == run.id)
            .values(undone=True)
        )
        run.status = RunStatus.UNDONE
        run.undone_at = _utcnow()
        record_run_event(
            session,
            user_id=user_id,
            run_id=run.id,
            type=RunEventType.UNDO_FINISHED,
            sentence=f"Undo complete — {reversed_count} changes restored. "
            "Gmail is back exactly as it was before this run.",
            detail={"reversed": reversed_count, "errors": errors},
        )
    else:
        record_run_event(
            session,
            user_id=user_id,
            run_id=run.id,
            type="undo_interrupted",
            sentence=(
                f"Undo stopped with {remaining} changes still to restore — "
                "press Undo again to continue where it left off."
            ),
            detail={"reversed": reversed_count, "remaining": remaining, "errors": errors},
        )
    session.commit()

    return {
        "run_id": run.id,
        "status": run.status,
        "reversed": reversed_count,
        "remaining": remaining,
        "errors": errors,
    }


def _undo_sentence(row: Mutation) -> str:
    if row.action == "add_label":
        return f"Removed label '{row.label_name}' from a thread."
    if row.action == "remove_label":
        return f"Restored label '{row.label_name}' on a thread."
    if row.action == "remove_inbox":
        return "Moved a thread back into the inbox."
    return "Archived a thread again (it was un-archived by this run)."
