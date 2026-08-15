"""Re-organise everything — every past decision, under the changed taxonomy.

The user's instruction, verbatim: *"Re-organise everything."* So the scope is
**every thread this user has ever had a decision about — ~10,336 on the live
account — including threads that are already archived**, which are relabelled in
place and never returned to the inbox. Not the current inbox. Not a sample.
Never a silent cap.

## The one invariant this module exists to keep

``done + sum(skipped.values()) == total``

Every row is either re-organised or **named** under exactly one reason from the
closed set ``db.models.REORG_SKIP_REASONS``. There is no "the rest". The ledger
is written to the database every batch, so the skipped-by-reason table is
readable **while the job runs**, not only at the end — a job that quietly stops
counting is indistinguishable from one that quietly stops working, and the user
would find out from Gmail rather than from us.

## How a thread's new category is resolved — and why there is no new classifier

Two tiers, in order, both of them the ones that already exist:

1. **Tier 1, the unchanged deterministic matcher** (:func:`tools.rules.apply_rules`).
   After discovery (slice 4) the dominant senders carry mined ``Rule`` rows, so
   most of the mailbox resolves here at rule confidence with **zero** tokens.
   This is the whole point of Phase 9's concentration work: the mail that used
   to be asked of the model is not asked of it any more.
2. **The genuine long tail goes through the existing graph** —
   :func:`graph.runner.execute_triage` with the items supplied, on the job's own
   run id. That means the *same* nodes, the *same* second-pass reviewer and the
   *same* ``finalise_review`` scope rules as an ordinary triage run. It is
   invoked with ``dry_run=True`` so the graph's own auto-apply pass never fires:
   the re-organiser owns the mutation pass, and it owns it through slice 3's
   functions only.

   Because that pass writes onto **one** run id for the whole job, resuming is
   the existing Phase 6 machinery: ``already_decided_item_ids`` drops everything
   already classified, so a resumed job re-classifies nothing and spends
   strictly fewer LLM calls than it did the first time.

If neither tier resolves a category, the thread is counted ``no_category_fit``
and left exactly as it is. Guessing would be worse than saying so.

**There is no third classification path here, and no generator may add one.**

## What it is not allowed to do

* It has **no private path to the mutator**. Every mutation goes through
  ``tools.actions`` (slice 3) — ``relabel_decision`` and
  ``archive_to_never_miss_label`` — which carry the ``NotReviewedError`` gate,
  the ``dry_run`` refusal and the undo token. If those functions are missing the
  job **fails loudly** rather than reporting a clean ledger over work it never
  did.
* It never passes ``force=True`` and never writes ``review_state``. A thread the
  reviewer never passed is counted ``not_reviewed`` and is not mutated — and if
  that count were ever removed, ``relabel_decision`` would still raise before the
  mutator, which is the guarantee (the count is the explanation, not the fence).
* It never deletes, trashes or spam-reports; the only operations that reach
  ``action_logs`` are ``archive`` / ``add_label`` / ``remove_label``.
* Under ``dry_run`` it performs **zero** Gmail mutations and still produces the
  complete ledger — every would-be mutation counted under ``dry_run``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from observability.events import get_logger

log = get_logger("reorg")

#: How many threads are read, resolved and mutated before the ledger is written
#: back. Small enough that progress is visibly live, large enough that the job
#: is not one transaction per thread over ten thousand threads.
BATCH_SIZE = 200

#: The no-silent-beat rule (Phase 7) applies unchanged: a ``reorg_progress``
#: event at least this often while the job runs, even when a batch is slow.
PROGRESS_INTERVAL_SECONDS = 3.0

#: Emitted at least this often by count as well as by clock, so a fast job still
#: streams rather than landing in one lump.
PROGRESS_EVERY = 50

#: Bounded backoff for a Gmail ``429``/``quotaExceeded`` that survived the
#: mutator's own retries. A rate limit is a wait, never a dropped thread — but
#: the wait is bounded so a job can never hang for ever.
RATE_LIMIT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0, 15.0)

#: Statuses from which a job will never do more work.
TERMINAL_STATUSES = ("completed", "partial", "cancelled", "failed")

#: Triage-run statuses that mean a second writer against this mailbox would be
#: racing the first. Two writers against one mailbox is not a supported state.
BLOCKING_RUN_STATUSES = ("running", "applying")


class ReorgError(RuntimeError):
    """A business-rule refusal from the re-organiser."""


class ReorgInProgress(ReorgError):
    """A re-organisation is already running for this user (``409``)."""


class RunInProgress(ReorgError):
    """A triage run is in flight for this user (``409``)."""


class MutationPathMissing(ReorgError):
    """``tools.actions`` does not expose the re-organiser's mutation functions.

    Raised — loudly, failing the job — rather than skipped over. A re-organiser
    that silently does nothing and reports a tidy ledger is the exact defect
    class this codebase keeps producing: plumbed, never wired.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _models():
    from db import models

    return models


def _emit(user_id: str, event: dict) -> None:
    """Fire-and-forget onto the SSE bus. Telemetry never fails a job."""
    try:
        from events import bus

        bus.emit(user_id, event)
    except Exception:  # pragma: no cover - the bus must never break the job
        pass


# --- ledger ---------------------------------------------------------------------


def _clean_skipped(raw: Any) -> dict[str, int]:
    """The persisted ``skipped`` map, filtered to the closed reason set.

    A reason outside :data:`db.models.REORG_SKIP_REASONS` would mean a thread was
    filed under a bucket nothing renders, which is a silent drop wearing a label.
    """
    m = _models()
    out: dict[str, int] = {}
    for reason in m.REORG_SKIP_REASONS:
        count = int((raw or {}).get(reason) or 0)
        if count:
            out[reason] = count
    return out


def ledger(session: Session, *, job_id: str) -> dict:
    """The job's ledger, read live from the row — never from a cached count.

    ``{"total", "done", "skipped": {reason: n}, "status", "undoable",
    "error_message"}``. ``done + sum(skipped.values()) == total`` holds on every
    read of a finished job, and is asserted in tests.
    """
    m = _models()
    job = session.get(m.ReorgJob, job_id)
    if job is None:
        raise ReorgError(f"reorg job {job_id!r} not found")

    undoable = bool(
        session.execute(
            select(func.count(m.ActionLog.id)).where(
                m.ActionLog.reorg_job_id == job_id,
                m.ActionLog.undone_at.is_(None),
            )
        ).scalar_one()
        or 0
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "total": int(job.total or 0),
        "done": int(job.done or 0),
        "skipped": _clean_skipped(job.skipped),
        "undoable": undoable,
        "dry_run": bool(job.dry_run),
        "error_message": job.error_message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


# --- starting -------------------------------------------------------------------


def active_job_id(session: Session, *, user_id: str) -> str | None:
    """The id of this user's in-flight re-organisation, or ``None``."""
    m = _models()
    return session.execute(
        select(m.ReorgJob.id)
        .where(m.ReorgJob.user_id == user_id, m.ReorgJob.status == "running")
        .order_by(m.ReorgJob.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def blocking_run_id(session: Session, *, user_id: str) -> str | None:
    """The id of a triage run that would be a second writer, or ``None``."""
    m = _models()
    return session.execute(
        select(m.TriageRun.id)
        .where(
            m.TriageRun.user_id == user_id,
            m.TriageRun.status.in_(BLOCKING_RUN_STATUSES),
        )
        .order_by(m.TriageRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def scope_total(session: Session, *, user_id: str) -> int:
    """How many threads "everything" actually means, counted — never estimated.

    One thread, not one decision row: a thread with three historic decisions is
    still one thread to re-file. This is the number the progress card divides by,
    so it is computed from the database up front and never adjusted downwards
    mid-job.
    """
    m = _models()
    return int(
        session.execute(
            select(func.count(func.distinct(m.Decision.item_id))).where(
                m.Decision.user_id == user_id
            )
        ).scalar_one()
        or 0
    )


def start(session: Session, *, user_id: str, dry_run: bool) -> str:
    """Create the job row and return its id. Does **no** work itself.

    Refuses with :class:`ReorgInProgress` / :class:`RunInProgress` rather than
    letting two writers loose on one mailbox. The caller (``api.reorganise``)
    schedules :func:`execute` in the background.
    """
    m = _models()

    existing = active_job_id(session, user_id=user_id)
    if existing:
        raise ReorgInProgress(
            f"a re-organisation is already running for this user (job {existing})"
        )
    run_id = blocking_run_id(session, user_id=user_id)
    if run_id:
        raise RunInProgress(
            f"a triage run is in flight (run {run_id}) — two writers against one "
            "mailbox is not supported. Wait for it to finish, then re-organise."
        )

    job = m.ReorgJob(
        user_id=user_id,
        status="running",
        total=scope_total(session, user_id=user_id),
        done=0,
        skipped={},
        cursor=None,
        dry_run=bool(dry_run),
        started_at=_now(),
    )
    session.add(job)
    session.flush()
    log.info(
        "reorg.started", job_id=job.id, user_id=user_id, total=job.total, dry_run=dry_run
    )
    _emit(
        user_id,
        {
            "type": "reorg_progress",
            "ts": time.time(),
            "job_id": job.id,
            "done": 0,
            "total": int(job.total or 0),
            "phase": "starting",
            "current_category": None,
        },
    )
    return job.id


def cancel(session: Session, *, job_id: str, user_id: str) -> dict:
    """Ask a running job to stop. Work already done stays done and stays undoable.

    Cancellation is a persisted flag the worker checks between threads, not an
    interrupt — so nothing is left half-applied.
    """
    m = _models()
    job = session.get(m.ReorgJob, job_id)
    if job is None or job.user_id != user_id:
        raise ReorgError(f"reorg job {job_id!r} not found")
    if job.status == "running":
        job.status = "cancelled"
        job.finished_at = _now()
        job.error_message = (
            "You cancelled this re-organisation. Everything it had already "
            "re-filed stayed re-filed, and all of it can still be undone."
        )
        session.flush()
    return ledger(session, job_id=job_id)


def _is_cancelled(session_factory, job_id: str) -> bool:
    """Re-read the flag from the database — a cached value can never see a cancel."""
    m = _models()
    try:
        with session_factory() as session:
            status = session.execute(
                select(m.ReorgJob.status).where(m.ReorgJob.id == job_id)
            ).scalar_one_or_none()
    except Exception:  # pragma: no cover - never fail the job over a status read
        return False
    return status == "cancelled"


# --- resolving the target category ----------------------------------------------


def _item_payload(item) -> dict:
    """The tier-1 matcher's view of a thread. Headers and ids only — never body.

    Mirrors ``graph.nodes._thread_payload``'s matcher-relevant fields so the
    unchanged ``tools.rules.matches`` sees exactly what it sees during triage.
    """
    return {
        "id": item.id,
        "external_thread_id": item.external_thread_id,
        "from_email": item.from_email or "",
        "from_domain": item.from_domain or "",
        "list_id": item.list_id,
        "subject": item.subject or "",
        "has_attachments": bool(item.has_attachments),
        "internal_date": item.internal_date,
        "channel_labels": list(item.channel_labels or []),
    }


def load_active_rules(session: Session, *, user_id: str) -> list[dict]:
    """The user's tier-1 rules, in the shape ``tools.rules.apply_rules`` expects.

    Read straight from ``rules`` — including the mined sender rules slice 4
    materialises — and handed to the **unchanged** matcher. The re-organiser does
    not reimplement matching; if the matcher and the re-organiser could disagree,
    a re-organisation would file mail differently from the triage run that
    follows it.
    """
    m = _models()
    rows = session.execute(
        select(m.Rule)
        .where(m.Rule.user_id == user_id, m.Rule.status.in_(("active", "automatic")))
        .order_by(m.Rule.confidence.desc(), m.Rule.created_at.asc())
    ).scalars()
    return [
        {
            "id": r.id,
            "name": r.name,
            "matcher": r.matcher or {},
            "action": r.action or {},
            "status": r.status,
            "confidence": r.confidence,
        }
        for r in rows
    ]


def _category_index(session: Session, *, user_id: str) -> tuple[dict, dict]:
    """``({key: Category}, {id: Category})`` for this user."""
    m = _models()
    rows = list(
        session.execute(
            select(m.Category).where(m.Category.user_id == user_id)
        ).scalars()
    )
    return {c.key: c for c in rows}, {c.id: c for c in rows}


def resolve_targets_tier1(items: list[dict], rules: list[dict]) -> dict[str, str]:
    """``{item_id: category_key}`` for every thread tier 1 resolves. Zero tokens.

    Calls :func:`tools.rules.apply_rules` directly — the same function
    ``graph.nodes.apply_deterministic_rules`` calls — so this is the existing
    seam being used, not a copy of it.
    """
    from tools.rules import apply_rules

    decisions, _unresolved = apply_rules(items, rules)
    out: dict[str, str] = {}
    for decision in decisions:
        category = decision.get("category")
        if category:
            out[str(decision["item_id"])] = str(category)
    return out


def reclassify_via_graph(
    *, user_id: str, channel_account_id: str, run_id: str, items: list[dict]
) -> dict[str, str]:
    """The long tail, through the existing graph and the existing reviewer.

    ``dry_run=True`` on purpose: the graph's own auto-apply pass must not fire,
    because the re-organiser owns the mutation pass and owns it through slice 3's
    functions. Everything else — the cascade, the second-pass reviewer,
    ``finalise_review``'s audited-only scope — is the ordinary path, unchanged.

    Returns ``{item_id: category_key}`` for whatever the graph resolved. A batch
    that fails resolves nothing and is reported by the caller as
    ``no_category_fit``; it never guesses and never fails the whole job.
    """
    if not items:
        return {}
    from graph.runner import execute_triage

    try:
        execute_triage(
            user_id=user_id,
            channel_account_id=channel_account_id,
            run_id=run_id,
            items=items,
            dry_run=True,
            kind="reorg",
            limit=len(items),
        )
    except Exception as exc:
        log.warning("reorg.reclassify_failed", run_id=run_id, error=str(exc))
        return {}

    m = _models()
    from db.session import create_db_session

    wanted = {str(i["id"]) for i in items}
    out: dict[str, str] = {}
    with create_db_session() as session:
        _by_key, by_id = _category_index(session, user_id=user_id)
        rows = session.execute(
            select(m.Decision).where(
                m.Decision.run_id == run_id,
                m.Decision.user_id == user_id,
                m.Decision.item_id.in_(sorted(wanted)),
            )
        ).scalars()
        for row in rows:
            category = by_id.get(row.category_id) if row.category_id else None
            if category is not None:
                out[str(row.item_id)] = category.key
    return out


# --- the mutation pass ----------------------------------------------------------


def _actions_module():
    from tools import actions

    return actions


def _require_mutation_functions() -> tuple[Callable, Callable]:
    """``(relabel_decision, archive_to_never_miss_label)`` or a loud failure.

    Slice 3 owns ``tools/actions.py``. If its functions are not there the job
    must not pretend: an absent mutation path is a failed job with a message that
    names it, never a green ledger over zero work.
    """
    actions = _actions_module()
    relabel = getattr(actions, "relabel_decision", None)
    never_miss = getattr(actions, "archive_to_never_miss_label", None)
    missing = [
        name
        for name, fn in (
            ("relabel_decision", relabel),
            ("archive_to_never_miss_label", never_miss),
        )
        if fn is None
    ]
    if missing:
        raise MutationPathMissing(
            "tools.actions is missing "
            + ", ".join(missing)
            + " — the re-organiser has no mutation path and refuses to report "
            "progress it did not make."
        )
    return relabel, never_miss


def _never_archive_keys() -> frozenset[str]:
    from tools.taxonomy import NEVER_ARCHIVE_KEYS

    return frozenset(NEVER_ARCHIVE_KEYS)


def _in_inbox(item) -> bool:
    from channels.gmail.mutations import INBOX_LABEL_ID

    return INBOX_LABEL_ID in set(item.channel_labels or [])


def _with_rate_limit_retry(call: Callable[[], Any], *, sleep=time.sleep) -> Any:
    """Run one mutation, backing off on a rate limit rather than dropping a thread.

    The mutator already retries ``429``/5xx internally; this is the outer, bounded
    wait for the case where the whole account ceiling is saturated by a job doing
    thousands of calls. Bounded on purpose — an unbounded wait is a hang.
    """
    from channels.base import RateLimited

    last: Exception | None = None
    for wait in (0.0, *RATE_LIMIT_BACKOFF_SECONDS):
        if wait:
            sleep(wait)
        try:
            return call()
        except RateLimited as exc:
            last = exc
            log.warning("reorg.rate_limited_backoff", wait_seconds=wait)
    raise last if last is not None else RuntimeError("unreachable")


def mutate_one(
    session: Session,
    *,
    user_id: str,
    job_id: str,
    decision,
    item,
    target_category,
    mutator,
    label_lookup,
    dry_run: bool,
    sleep=time.sleep,
) -> str:
    """Re-file one thread. Returns ``"done"`` or a skip reason.

    Three shapes, and only three:

    * **already archived** → ``relabel_decision(keep_archived=True)``: the old
      ``ZeroInbox/*`` label comes off, the new one goes on, and ``INBOX`` is
      **never** re-added. A re-organisation must not push a thousand archived
      threads back into the inbox.
    * **in the inbox, held by never-miss, new category is a never-miss one** →
      ``archive_to_never_miss_label``: the Phase 9 reframe applied retroactively.
      It refuses to run without a resolved label, and that refusal is the
      guarantee that nothing is archived unlabelled.
    * **in the inbox, otherwise** → ``relabel_decision(keep_archived=False)``: the
      label changes; whether the thread leaves the inbox is triage's call under
      the autonomy policy, not the re-organiser's.

    ``review_state`` is never written here and ``force=True`` is never passed.
    The ``not_reviewed`` pre-check below is the *explanation*; the fence is
    ``NotReviewedError`` inside ``tools.actions``, which fires before the mutator
    either way.
    """
    from channels.base import ChannelError, DryRunViolation
    from tools.actions import ActionsError, NotReviewedError

    relabel_decision, archive_to_never_miss_label = _require_mutation_functions()

    if getattr(decision, "review_state", "reviewed") != "reviewed":
        # Counted, named, and left completely alone. Never upgraded to get past
        # the gate — that is the defect Phase 9 item zero exists to close.
        return "not_reviewed"

    if str(decision.category_id or "") == str(target_category.id):
        return "already_correct"

    if dry_run:
        # Absolute. No credentials used, no call attempted, full ledger still
        # produced — the preview is the product, not a placeholder.
        return "dry_run"

    archived_already = not _in_inbox(item)

    # One SAVEPOINT per thread. Without it, a single Gmail refusal rolls the
    # session back to the last batch commit and silently discards every
    # ``ActionLog`` written since — up to 199 real, already-executed Gmail
    # mutations left with no undo token, while the ledger still counted them
    # ``done``. Measured: 4,000 mutations, 1,460 undoable. One thread's failure
    # must undo one thread's bookkeeping and nothing else.
    savepoint = session.begin_nested()

    # The re-classification result is written onto the decision BEFORE the
    # mutation, so the ActionLog and the decision can never disagree about which
    # label the thread was given.
    decision.category_id = target_category.id
    decision.status = "approved"
    if archived_already and decision.proposed_action not in ("archive", "digest"):
        # An already-archived thread's mutation is a relabel; the action must
        # still be a mutable one for the gate's own scope check to make sense.
        decision.proposed_action = "archive"
    session.flush()

    never_miss = (
        not archived_already
        and getattr(decision, "autonomy_state", None) == "held_by_never_miss"
        and target_category.key in _never_archive_keys()
    )

    def _call():
        if never_miss:
            return archive_to_never_miss_label(
                session,
                user_id,
                decision.id,
                mutator=mutator,
                label_lookup=label_lookup,
                dry_run=False,
            )
        return relabel_decision(
            session,
            user_id,
            decision.id,
            mutator=mutator,
            label_lookup=label_lookup,
            dry_run=False,
            keep_archived=archived_already,
        )

    try:
        action_log = _with_rate_limit_retry(_call, sleep=sleep)
    except NotReviewedError:
        savepoint.rollback()
        return "not_reviewed"
    except (ActionsError, ChannelError, DryRunViolation) as exc:
        savepoint.rollback()
        log.warning(
            "reorg.thread_failed",
            job_id=job_id,
            decision_id=str(decision.id),
            error=f"{type(exc).__name__}: {exc}",
        )
        return "gmail_error"
    except Exception as exc:  # one thread's failure never blocks the rest
        savepoint.rollback()
        log.warning(
            "reorg.thread_unexpected_error",
            job_id=job_id,
            decision_id=str(decision.id),
            error=f"{type(exc).__name__}: {exc}",
        )
        return "gmail_error"

    # Stamp the mutation with the job so bulk undo is one indexed query. Done
    # here rather than inside tools.actions because `action_logs.reorg_job_id` is
    # this slice's column and slice 3's functions know nothing about jobs.
    if action_log is not None and hasattr(action_log, "reorg_job_id"):
        action_log.reorg_job_id = job_id
    if action_log is None:  # pragma: no cover - slice 3 always returns the row
        savepoint.rollback()
        return "gmail_error"
    if getattr(action_log, "undo_token", None) is None:
        # Every mutation carries an undo token. A mutation we cannot reverse is
        # not an acceptable outcome, so it is reported rather than counted done.
        log.error(
            "reorg.mutation_without_undo_token", job_id=job_id, decision_id=str(decision.id)
        )
        savepoint.rollback()
        return "gmail_error"
    savepoint.commit()
    return "done"


# --- the worker -----------------------------------------------------------------


def _scope_rows(session: Session, *, user_id: str, after_item_id: str | None):
    """Every thread in scope, newest decision first per thread, ordered by item id.

    Ordering by ``item_id`` is what makes ``cursor`` meaningful: a resumed job
    asks for ``item_id > cursor`` and therefore re-does nothing, without holding
    ten thousand ids in memory.
    """
    m = _models()
    stmt = select(m.Decision).where(m.Decision.user_id == user_id)
    if after_item_id:
        stmt = stmt.where(m.Decision.item_id > after_item_id)
    stmt = stmt.order_by(m.Decision.item_id.asc(), m.Decision.created_at.desc())
    seen: set[str] = set()
    for row in session.execute(stmt).scalars():
        if row.item_id in seen:
            continue  # an older decision for a thread already taken
        seen.add(row.item_id)
        yield row


class _Progress:
    """Emits ``reorg_progress`` at least every 3 s. The no-silent-beat rule."""

    def __init__(self, user_id: str, job_id: str, total: int) -> None:
        self.user_id = user_id
        self.job_id = job_id
        self.total = total
        self._last = 0.0
        self._last_done = -1

    def beat(self, *, done: int, phase: str, current_category: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        due = (
            force
            or now - self._last >= PROGRESS_INTERVAL_SECONDS
            or done - self._last_done >= PROGRESS_EVERY
        )
        if not due:
            return
        self._last = now
        self._last_done = done
        _emit(
            self.user_id,
            {
                "type": "reorg_progress",
                "ts": time.time(),
                "job_id": self.job_id,
                "done": done,
                "total": self.total,
                "phase": phase,
                "current_category": current_category,
            },
        )


def _finish(session: Session, job, *, cancelled: bool) -> None:
    """Close the job out honestly: ``partial`` whenever anything was skipped."""
    skipped = _clean_skipped(job.skipped)
    total_skipped = sum(skipped.values())
    if cancelled:
        job.status = "cancelled"
        job.error_message = (
            "You cancelled this re-organisation. Everything it had already "
            "re-filed stayed re-filed, and all of it can still be undone."
        )
    elif total_skipped:
        # `completed` never hides a skip. If one thread out of ten thousand was
        # not re-filed, this job did not complete — it was partial, and it says
        # which threads and why.
        job.status = "partial"
        job.error_message = _partial_sentence(skipped)
    else:
        job.status = "completed"
        job.error_message = None
    job.finished_at = _now()
    session.flush()


#: Plain-English names for the skip reasons — the ledger is read by a person.
SKIP_REASON_WORDS: dict[str, str] = {
    "not_reviewed": "never passed the never-miss reviewer, so they were not touched",
    "no_category_fit": "matched no category in the new taxonomy",
    "gmail_error": "Gmail refused the change",
    "already_correct": "were already filed under the right label",
    "dry_run": "would have been re-filed — this was a preview, so nothing was changed",
    "cancelled": "were not reached before you cancelled",
}


def _partial_sentence(skipped: dict[str, int]) -> str:
    parts = [
        f"{count} {SKIP_REASON_WORDS.get(reason, reason)}"
        for reason, count in skipped.items()
        if count
    ]
    return "Not everything was re-organised: " + "; ".join(parts) + "."


def execute(
    job_id: str,
    *,
    user_id: str,
    session_factory: Callable[[], Session] | None = None,
    mutator=None,
    label_lookup=None,
    channel_account_id: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Run the job to completion (or to its cancellation). Never raises.

    ``mutator`` / ``label_lookup`` are injected by tests and built from the
    user's stored credentials otherwise — the same seam
    ``graph.nodes._build_mutator_for_user`` provides for the triage apply pass,
    so there is exactly one way to reach Gmail in this codebase.
    """
    if session_factory is None:
        from db.session import create_db_session

        session_factory = create_db_session

    m = _models()
    try:
        return _execute(
            job_id,
            user_id=user_id,
            session_factory=session_factory,
            mutator=mutator,
            label_lookup=label_lookup,
            channel_account_id=channel_account_id,
            sleep=sleep,
        )
    except Exception as exc:
        # A job that could not finish says so — it never reports `completed`.
        log.error("reorg.failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}")
        try:
            with session_factory() as session:
                job = session.get(m.ReorgJob, job_id)
                if job is not None and job.status == "running":
                    job.status = "failed"
                    job.error_message = (
                        "This re-organisation stopped before it finished: "
                        f"{type(exc).__name__}: {exc}. Nothing was left half-applied, "
                        "and everything it had already done can still be undone."
                    )
                    job.finished_at = _now()
                session.commit()
            with session_factory() as session:
                payload = ledger(session, job_id=job_id)
        except Exception:  # pragma: no cover - the failure path must not fail
            payload = {"job_id": job_id, "status": "failed", "error_message": str(exc)}
        _emit(
            user_id,
            {
                "type": "reorg_finished",
                "ts": time.time(),
                "job_id": job_id,
                "status": payload.get("status"),
                "done": payload.get("done", 0),
                "skipped": payload.get("skipped", {}),
            },
        )
        return payload


def _execute(
    job_id: str,
    *,
    user_id: str,
    session_factory,
    mutator,
    label_lookup,
    channel_account_id: str | None,
    sleep,
) -> dict:
    m = _models()

    with session_factory() as session:
        job = session.get(m.ReorgJob, job_id)
        if job is None or job.user_id != user_id:
            raise ReorgError(f"reorg job {job_id!r} not found")
        if job.status in TERMINAL_STATUSES:
            return ledger(session, job_id=job_id)
        dry_run = bool(job.dry_run)
        total = int(job.total or 0)
        cursor = job.cursor
        done = int(job.done or 0)
        counts = _clean_skipped(job.skipped)
        # `cancelled` is DERIVED — "how many we never reached" — not observed per
        # thread. A resumed job is about to reach some of them, so a stale count
        # carried over from the interrupted pass would double-count and break
        # `done + sum(skipped) == total`. It is recomputed at the end, never
        # accumulated.
        counts.pop("cancelled", None)
        if not job.run_id:
            # One run id for the whole job, created once: the graph's own resume
            # machinery then makes re-classification idempotent for free.
            from uuid import uuid4

            job.run_id = str(uuid4())
        run_id = job.run_id
        if channel_account_id is None:
            channel_account_id = session.execute(
                select(m.ChannelAccount.id)
                .where(m.ChannelAccount.user_id == user_id)
                .order_by(m.ChannelAccount.connected_at.asc())
                .limit(1)
            ).scalar_one_or_none()
        session.commit()

    if not dry_run and mutator is None:
        from db.session import create_db_session
        from graph.nodes import _build_mutator_for_user

        with create_db_session() as session:
            mutator, label_lookup = _build_mutator_for_user(
                user_id, channel_account_id, session
            )

    if not dry_run:
        # Fail before the first thread rather than after the ten thousandth.
        _require_mutation_functions()

    progress = _Progress(user_id, job_id, total)
    progress.beat(done=done, phase="resolving", force=True)
    cancelled = False

    while True:
        with session_factory() as session:
            # `islice` over the generator, never a materialised list of every
            # batch: "re-organise everything" must not mean "load everything".
            batch = list(
                islice(
                    _scope_rows(session, user_id=user_id, after_item_id=cursor),
                    BATCH_SIZE,
                )
            )
            if not batch:
                break
            # Detach to plain ids immediately. The batch outlives this session
            # (the long-tail re-classification runs outside it), and a lazily
            # refreshed ORM attribute on a closed session is a DetachedInstanceError
            # that would fail the whole job over a bookkeeping detail.
            batch = [(row.id, row.item_id) for row in batch]

            item_ids = [item_id for _decision_id, item_id in batch]
            items = {
                i.id: i
                for i in session.execute(
                    select(m.Item).where(m.Item.id.in_(item_ids))
                ).scalars()
            }
            rules = load_active_rules(session, user_id=user_id)
            by_key, _by_id = _category_index(session, user_id=user_id)

            payloads = [
                _item_payload(items[item_id])
                for _decision_id, item_id in batch
                if item_id in items
            ]
            targets = resolve_targets_tier1(payloads, rules)

            unresolved = [p for p in payloads if str(p["id"]) not in targets]
            session.commit()

        # The long tail, outside the session: it makes real LLM calls through the
        # existing graph, which opens its own sessions.
        if unresolved and channel_account_id:
            targets.update(
                reclassify_via_graph(
                    user_id=user_id,
                    channel_account_id=channel_account_id,
                    run_id=run_id,
                    items=unresolved,
                )
            )

        with session_factory() as session:
            job = session.get(m.ReorgJob, job_id)
            by_key, _by_id = _category_index(session, user_id=user_id)
            # Checked once per batch, not once per thread: a cancel is honoured
            # within one batch, and a per-row round trip would be 10,000 extra
            # queries for a flag that changes at most once.
            if _is_cancelled(session_factory, job_id):
                cancelled = True
            for decision_id, row_item_id in batch:
                if cancelled:
                    break
                decision = session.get(m.Decision, decision_id)
                item = session.get(m.Item, row_item_id)
                if decision is None or item is None:
                    counts["no_category_fit"] = counts.get("no_category_fit", 0) + 1
                    cursor = row_item_id
                    continue
                key = targets.get(str(row_item_id))
                target = by_key.get(key) if key else None
                if target is None:
                    counts["no_category_fit"] = counts.get("no_category_fit", 0) + 1
                else:
                    outcome = mutate_one(
                        session,
                        user_id=user_id,
                        job_id=job_id,
                        decision=decision,
                        item=item,
                        target_category=target,
                        mutator=mutator,
                        label_lookup=label_lookup,
                        dry_run=dry_run,
                        sleep=sleep,
                    )
                    if outcome == "done":
                        done += 1
                    else:
                        counts[outcome] = counts.get(outcome, 0) + 1
                cursor = row_item_id
                job.done = done
                job.skipped = dict(counts)
                job.cursor = cursor
                progress.beat(
                    done=done,
                    phase="applying",
                    current_category=(target.name if target is not None else None),
                )
            job.done = done
            job.skipped = dict(counts)
            job.cursor = cursor
            session.commit()

        if cancelled:
            break

    with session_factory() as session:
        job = session.get(m.ReorgJob, job_id)
        job.done = done
        # Whatever the cursor never reached is `cancelled` — named, not dropped.
        # This is the line that keeps done + sum(skipped) == total true for a job
        # that stopped early.
        counts.pop("cancelled", None)
        unreached = max(total - done - sum(counts.values()), 0)
        if unreached:
            counts["cancelled"] = unreached
        job.skipped = dict(counts)
        job.cursor = cursor
        _finish(session, job, cancelled=cancelled)
        payload = ledger(session, job_id=job_id)
        session.commit()

    progress.beat(done=done, phase="finished", force=True)
    _emit(
        user_id,
        {
            "type": "reorg_finished",
            "ts": time.time(),
            "job_id": job_id,
            "status": payload["status"],
            "done": payload["done"],
            "skipped": payload["skipped"],
        },
    )
    log.info(
        "reorg.finished",
        job_id=job_id,
        status=payload["status"],
        total=payload["total"],
        done=payload["done"],
        skipped=payload["skipped"],
    )
    return payload


# --- bulk undo ------------------------------------------------------------------


def undo(session: Session, *, job_id: str, user_id: str, mutator) -> dict:
    """Undo the whole re-organisation as ONE operation. Idempotent.

    One indexed query over ``action_logs.reorg_job_id``, reversed in insertion
    order, restoring each thread's **exact pre-job label set** from its own undo
    token. A second call performs **zero** Gmail calls and returns the same
    counts — the local ``undone_at`` stamp is the idempotency guard, so undo can
    never double-mutate a mailbox.

    Returns ``{"reversed", "already_undone", "failed": [{thread_id, reason}]}``.
    """
    m = _models()
    job = session.get(m.ReorgJob, job_id)
    if job is None or job.user_id != user_id:
        raise ReorgError(f"reorg job {job_id!r} not found")

    rows = list(
        session.execute(
            select(m.ActionLog)
            .where(m.ActionLog.reorg_job_id == job_id, m.ActionLog.user_id == user_id)
            .order_by(m.ActionLog.created_at.desc(), m.ActionLog.id.desc())
        ).scalars()
    )

    reversed_count = 0
    already_undone = 0
    failed: list[dict] = []

    for row in rows:
        token = row.undo_token or {}
        thread_id = token.get("thread_id")
        if row.undone_at is not None:
            already_undone += 1
            continue  # zero Gmail calls on the second pass
        if not thread_id:
            failed.append(
                {"thread_id": None, "reason": "undo token is missing the thread id"}
            )
            continue
        original_label_ids = token.get("original_label_ids")
        labels_added = [
            lb
            for lb in (token.get("labels_added_by_triage") or [token.get("category_label_id")])
            if lb
        ]
        try:
            if original_label_ids is not None:
                mutator.restore_labels(
                    thread_id,
                    add_label_ids=list(original_label_ids),
                    remove_label_ids=labels_added,
                )
            else:
                category_label_id = token.get("category_label_id")
                if not category_label_id:
                    failed.append(
                        {
                            "thread_id": thread_id,
                            "reason": "undo token has neither a label snapshot nor a category label",
                        }
                    )
                    continue
                mutator.undo_archive_and_label(
                    thread_id, category_label_id=category_label_id
                )
        except Exception as exc:  # ChannelError and anything else — named, never dropped
            failed.append({"thread_id": thread_id, "reason": f"{type(exc).__name__}: {exc}"})
            continue

        row.undone_at = _now()
        if row.decision_id:
            decision = session.get(m.Decision, row.decision_id)
            if decision is not None and decision.user_id == user_id:
                decision.status = "undone"
        item = session.execute(
            select(m.Item).where(
                m.Item.external_thread_id == thread_id, m.Item.user_id == user_id
            )
        ).scalar_one_or_none()
        if item is not None and original_label_ids is not None:
            item.channel_labels = sorted(original_label_ids)
        reversed_count += 1
        session.flush()

    log.info(
        "reorg.undone",
        job_id=job_id,
        reversed=reversed_count,
        already_undone=already_undone,
        failed=len(failed),
    )
    return {
        "reversed": reversed_count,
        "already_undone": already_undone,
        "failed": failed,
    }


__all__ = [
    "BATCH_SIZE",
    "PROGRESS_INTERVAL_SECONDS",
    "MutationPathMissing",
    "ReorgError",
    "ReorgInProgress",
    "RunInProgress",
    "active_job_id",
    "blocking_run_id",
    "cancel",
    "execute",
    "ledger",
    "load_active_rules",
    "mutate_one",
    "reclassify_via_graph",
    "resolve_targets_tier1",
    "scope_total",
    "start",
    "undo",
]
