"""Seeds ONE throwaway Gmail thread + a matching, UI-visible `proposed` Decision row,
for the Phase 2 e2e actions.spec.ts to approve/undo — so that suite never touches a
real, pre-existing inbox message.

Mirrors tests/integration/test_gmail_mutations.py's ``_insert_test_thread`` /
``action_pipeline_row`` convention exactly: the message is inserted directly via
``users().messages().insert()`` (never sent), tagged with a unique subject so it is
unambiguously identifiable in the UI, and wired to a real Category/Cluster/Item/
Decision row so the queue can approve and undo it for real.

Usage: `uv run python tests/e2e/phase2/seed_test_thread.py`
Prints one line of JSON: {"subject_tag", "decision_id", "thread_id", "user_id"}.
Skips (prints {"skipped": true, "reason": ...}) if no mailbox is connected.
"""

from __future__ import annotations

import base64
import json
import sys
import uuid
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


def _connection():
    from sqlalchemy import create_engine, text

    from config.settings import get_settings
    from security.crypto import CryptoError, TokenCipher

    url = get_settings().database_url.replace("+aiosqlite", "")
    engine = create_engine(url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT user_id, account_email, refresh_token_enc, id "
                "FROM channel_accounts "
                "WHERE channel = 'gmail' AND refresh_token_enc IS NOT NULL "
                "AND refresh_token_enc != '' "
                "ORDER BY connected_at DESC LIMIT 1"
            )
        ).first()
    if row is None:
        return None
    try:
        refresh_token = TokenCipher().decrypt(row[2])
    except CryptoError:
        return None
    return {"user_id": row[0], "account_email": row[1], "refresh_token": refresh_token, "channel_account_id": row[3]}


def main() -> None:
    conn = _connection()
    if conn is None:
        print(json.dumps({"skipped": True, "reason": "no mailbox connected"}))
        return

    from googleapiclient.discovery import build

    from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config

    config = google_oauth_config()
    credentials = credentials_from_refresh_token(config, conn["refresh_token"])
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    tag = f"e2e-actions-{uuid.uuid4().hex[:8]}"
    subject = f"[zero-inbox-agent test] {tag}"
    message = MIMEText("This is a throwaway message created by an automated e2e test.")
    message["To"] = conn["account_email"] or "me"
    message["From"] = conn["account_email"] or "me"
    message["Subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    inserted = (
        service.users()
        .messages()
        .insert(userId="me", body={"raw": raw, "labelIds": ["INBOX"]})
        .execute()
    )
    thread_id = inserted["threadId"]

    import datetime as dt

    import db.session as session_module
    from db.models import Category, Cluster, Decision, Item, TriageRun

    with session_module._SessionLocal() as session:
        category = (
            session.query(Category)
            .filter(Category.user_id == conn["user_id"], Category.key == "e2e-actions-test")
            .first()
        )
        if category is None:
            category = Category(
                user_id=conn["user_id"],
                key="e2e-actions-test",
                name="E2EActionsTest",
                description="throwaway category for actions.spec.ts",
                channel_label_name="ZeroInbox/E2EActionsTest",
                default_action="archive",
            )
            session.add(category)
            session.flush()

        run = TriageRun(
            user_id=conn["user_id"],
            channel_account_id=conn["channel_account_id"],
            kind="incremental",
            status="completed",
            dry_run=False,
            items_total=1,
            items_decided=1,
        )
        session.add(run)
        session.flush()

        cluster = Cluster(
            user_id=conn["user_id"],
            run_id=run.id,
            kind="sender",
            label=subject,
            item_count=1,
            suggested_action="archive",
            min_confidence=0.99,
            avg_confidence=0.99,
        )
        session.add(cluster)
        session.flush()

        item = Item(
            user_id=conn["user_id"],
            channel_account_id=conn["channel_account_id"],
            external_thread_id=thread_id,
            external_message_ids=[thread_id],
            subject=subject,
            from_email=conn["account_email"] or "me@example.com",
            from_domain=(conn["account_email"] or "example.com").split("@")[-1],
            snippet_redacted="throwaway e2e test message",
            internal_date=dt.datetime.now(dt.timezone.utc),
            channel_labels=["INBOX"],
        )
        session.add(item)
        session.flush()

        decision = Decision(
            user_id=conn["user_id"],
            item_id=item.id,
            run_id=run.id,
            cluster_id=cluster.id,
            category_id=category.id,
            proposed_action="archive",
            confidence=0.99,
            reasoning="e2e test seed — always archived",
            decided_by="rule",
            status="proposed",
        )
        session.add(decision)
        session.commit()

        print(
            json.dumps(
                {
                    "subject_tag": tag,
                    "subject": subject,
                    "decision_id": decision.id,
                    "cluster_id": cluster.id,
                    "thread_id": thread_id,
                    "user_id": conn["user_id"],
                }
            )
        )


if __name__ == "__main__":
    main()
