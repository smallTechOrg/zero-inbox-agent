"""The silent-abort **cause proof** — read-only, zero writes, zero mutations.

Run ``fbeed060`` (2,176 threads, ``dry_run=0``, status ``completed``) left all 615 of
its archive proposals at ``status='proposed'``. Every row untouched means
``_auto_apply_decisions`` aborted **before** its per-decision loop, and only two paths
did that: ``_build_mutator_for_user`` raising, or the outer ``except``.

This module proves which — it does not assume. It loads the REAL connected account's
refresh token, builds the mutator exactly as the apply pass does, and makes one
read-only ``labels().list()`` call. **Nothing is archived, labelled, modified or
written.** If no mailbox is connected it SKIPS — treat that skip as BLOCKED, not as a
pass: the cause is then unproven.

If this test FAILS, that failure IS the proven root cause. Record it in the report and
in an inline comment at the failure site in ``src/graph/nodes.py``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

SKIP_REASON = (
    "No Gmail mailbox connected — the silent-abort cause cannot be proven without a "
    "REAL connection. Run `uv run python -m src`, open http://localhost:8001/app/ and "
    "click 'Connect Gmail' (or set AGENT_TEST_GMAIL_REFRESH_TOKEN in .env). "
    "Treat this skip as BLOCKED, not as a pass."
)


def _connection_from_production_db() -> dict | None:
    """One read-only SELECT against the production DB. Never writes, never mutates."""
    from sqlalchemy import create_engine, text

    from config.settings import get_settings

    url = get_settings().database_url.replace("+aiosqlite", "")
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, user_id, account_email, refresh_token_enc "
                    "FROM channel_accounts "
                    "WHERE channel = 'gmail' AND refresh_token_enc IS NOT NULL "
                    "AND refresh_token_enc != '' "
                    "ORDER BY connected_at DESC LIMIT 1"
                )
            ).first()
        engine.dispose()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "connection_id": row[0],
        "user_id": row[1],
        "account_email": row[2],
        "refresh_token_enc": row[3],
    }


@pytest.fixture(scope="module")
def real_connection() -> dict:
    plain = os.environ.get("AGENT_TEST_GMAIL_REFRESH_TOKEN", "")
    if plain:
        return {
            "connection_id": "diag-conn",
            "user_id": "diag-user",
            "account_email": "",
            "refresh_token_enc": None,
            "refresh_token": plain,
        }
    found = _connection_from_production_db()
    if found is None:
        pytest.skip(SKIP_REASON)
    return found


def test_the_stored_refresh_token_decrypts(real_connection):
    """Cause candidate 1: the Fernet key rotated and the token no longer decrypts."""
    if real_connection.get("refresh_token"):
        pytest.skip("using AGENT_TEST_GMAIL_REFRESH_TOKEN — nothing to decrypt")
    from security.crypto import TokenCipher

    token = TokenCipher().decrypt(real_connection["refresh_token_enc"])
    assert token, "the stored refresh token decrypted to an empty string"


def test_build_mutator_for_user_succeeds_against_the_real_account(
    _isolated_db, real_connection
):
    """Cause candidate 2: `_build_mutator_for_user` raises — the abort path.

    The connection row is copied into the ISOLATED test database (never the
    production one) so the real code path — ``SqlConnectionStore.load_refresh_token``
    -> OAuth config -> credential refresh -> ``build('gmail')`` — runs end to end.
    """
    from db.models import ChannelAccount, User
    from db.session import create_db_session
    from graph.nodes import _build_mutator_for_user
    from security.crypto import TokenCipher

    enc = real_connection.get("refresh_token_enc") or TokenCipher().encrypt(
        real_connection["refresh_token"]
    )
    with create_db_session() as session:
        session.add(
            User(
                id=real_connection["user_id"],
                email=real_connection["account_email"] or "diag@example.com",
                display_name="Diagnosis",
            )
        )
        session.add(
            ChannelAccount(
                id=real_connection["connection_id"],
                user_id=real_connection["user_id"],
                channel="gmail",
                account_email=real_connection["account_email"] or "diag@example.com",
                refresh_token_enc=enc,
                scopes=[],
                status="connected",
            )
        )
        session.commit()

    with create_db_session() as session:
        mutator, label_lookup = _build_mutator_for_user(
            real_connection["user_id"], real_connection["connection_id"], session
        )

    assert mutator is not None
    assert label_lookup is not None

    # One READ-ONLY Gmail call. `labels().list()` mutates nothing.
    service = getattr(mutator, "_service", None) or getattr(mutator, "service", None)
    assert service is not None, "the mutator exposes no Gmail service to read with"
    response = service.users().labels().list(userId="me").execute()
    assert "labels" in response
