"""Applying approved decisions to the real mailbox, and undoing them.

spec/api.md § Phase 2:
    POST /api/actions/apply           {decision_ids: [...]} -> [{action_log_id, undo_token_id}]
    POST /api/actions/{action_log_id}/undo
    GET  /api/actions                 the audit trail, newest first

``undo_token_id`` is the ``action_log_id`` itself — undo is always addressed by the
action log row (``POST /api/actions/{action_log_id}/undo``), and every mutation this
module performs always carries a non-null ``undo_token`` (spec/data.md), so the two
ids are always the same non-null value.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import (
    DRY_RUN_VIOLATION,
    NOT_FOUND,
    NOT_REVIEWED,
    PROVIDER_ERROR,
    VALIDATION_ERROR,
    api_error,
    iso,
    not_found,
    ok,
)
from api.session import require_user_id
from channels.base import ChannelError, DryRunViolation
from db.session import get_session
from tools.actions import ActionsError, NeedsYourCallError, NotApprovedError, NotReviewedError, apply_decision, undo_action

router = APIRouter()


class ApplyRequest(BaseModel):
    decision_ids: list[str]
    force: bool = Field(
        default=False,
        description=(
            "Archive even 'keep'-proposed decisions. Off by default — only set "
            "this for a deliberate, user-initiated override; never from an "
            "ordinary bulk-approve path."
        ),
    )


def _action_log_payload(row) -> dict:
    return {
        "id": row.id,
        "decision_id": row.decision_id,
        "operation": row.operation,
        "request_params": row.request_params,
        "response": row.response,
        "undo_token": row.undo_token,
        "undone_at": iso(row.undone_at),
        "created_at": iso(row.created_at),
    }


def _mutator_and_labels_for_user(session: Session, user_id: str):
    """Builds a ``GmailMutator`` + ``GmailLabelManager`` for the user's connected mailbox.

    Mirrors ``api/categories.py``'s ``_label_manager_for_user`` so this route has no
    import-time coupling to the parallel categories/connections slices.
    """
    from googleapiclient.discovery import build

    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config
    from channels.gmail.store import SqlConnectionStore
    from db.models import ChannelAccount

    account = (
        session.query(ChannelAccount)
        .filter(ChannelAccount.user_id == user_id)
        .order_by(ChannelAccount.connected_at.desc())
        .first()
    )
    if account is None:
        raise api_error(VALIDATION_ERROR, "no connected mailbox to apply actions against")

    refresh_token = SqlConnectionStore().load_refresh_token(
        user_id=user_id, connection_id=account.id
    )
    config = google_oauth_config()
    credentials = credentials_from_refresh_token(config, refresh_token)
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GmailMutator(service), GmailLabelManager(service)


def _dry_run_for_user(session: Session, user_id: str) -> bool:
    from db.models import UserSettings

    row = session.get(UserSettings, user_id)
    return True if row is None else bool(row.dry_run)


@router.post("/api/actions/apply")
def apply_actions_route(
    body: ApplyRequest,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    if not body.decision_ids:
        raise api_error(VALIDATION_ERROR, "decision_ids is required")

    if _dry_run_for_user(session, user_id):
        raise api_error(
            DRY_RUN_VIOLATION, "dry_run is on — no mutation was attempted", 409
        )

    mutator, label_lookup = _mutator_and_labels_for_user(session, user_id)

    results: list[dict] = []
    first_error: Exception | None = None

    for decision_id in body.decision_ids:
        try:
            action_log = apply_decision(
                session,
                user_id,
                decision_id,
                mutator=mutator,
                label_lookup=label_lookup,
                dry_run=False,
                force=body.force,
            )
        except DryRunViolation as exc:
            session.rollback()
            first_error = first_error or api_error(DRY_RUN_VIOLATION, str(exc), 409)
        except NeedsYourCallError as exc:
            session.rollback()
            first_error = first_error or api_error(VALIDATION_ERROR, str(exc))
        except NotApprovedError as exc:
            session.rollback()
            first_error = first_error or api_error(VALIDATION_ERROR, str(exc))
        except NotReviewedError as exc:
            # MUST precede the ActionsError catch-all: NotReviewedError subclasses
            # it, so the generic handler would render this as 404 not_found — for a
            # decision that plainly exists and is merely still provisional.
            session.rollback()
            first_error = first_error or api_error(NOT_REVIEWED, str(exc), 422)
        except ActionsError as exc:
            session.rollback()
            first_error = first_error or api_error(NOT_FOUND, str(exc), 404)
        except ChannelError as exc:
            session.rollback()
            first_error = first_error or api_error(PROVIDER_ERROR, str(exc), 502)
        else:
            session.commit()
            results.append({"action_log_id": action_log.id, "undo_token_id": action_log.id})

    if not results and first_error is not None:
        raise first_error

    return ok(results)


@router.post("/api/actions/{action_log_id}/undo")
def undo_action_route(
    action_log_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    mutator, _label_lookup = _mutator_and_labels_for_user(session, user_id)
    try:
        action_log = undo_action(session, user_id, action_log_id, mutator=mutator)
    except ActionsError as exc:
        raise not_found("Action log") from exc
    except ChannelError as exc:
        raise api_error(PROVIDER_ERROR, str(exc), 502) from exc
    return ok(_action_log_payload(action_log))


@router.get("/api/actions")
def list_actions_route(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import ActionLog

    rows = (
        session.execute(
            select(ActionLog)
            .where(ActionLog.user_id == user_id)
            .order_by(ActionLog.created_at.desc())
        )
        .scalars()
        .all()
    )
    return ok([_action_log_payload(row) for row in rows])
