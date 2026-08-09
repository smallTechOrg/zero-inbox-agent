"""Persistence of a connected mailbox: users + channel_accounts.

The refresh token is Fernet-encrypted *before* it reaches the database and is
never returned, logged, or included in an error message.

`src/db/models.py` is owned by the db-schema slice; it is imported lazily so
this module has no import-time coupling to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from channels.base import ChannelError, ReauthRequired
from security.crypto import TokenCipher


class SchemaNotReady(ChannelError):
    """The db-schema slice has not provided `User` / `ChannelAccount` yet."""


def _models():
    try:
        from db import models
    except Exception as exc:  # pragma: no cover - db package always importable
        raise SchemaNotReady("db.models is not importable") from exc
    user = getattr(models, "User", None)
    account = getattr(models, "ChannelAccount", None)
    if user is None or account is None:
        raise SchemaNotReady(
            "db.models.User / db.models.ChannelAccount are not defined yet "
            "(owned by the db-schema slice) — run alembic upgrade head"
        )
    return user, account


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlConnectionStore:
    """Upserts the user + their connected mailbox, keyed by (email, channel)."""

    def __init__(self, cipher: TokenCipher | None = None) -> None:
        self._cipher = cipher or TokenCipher()

    def encrypt(self, refresh_token: str) -> str:
        return self._cipher.encrypt(refresh_token)

    def upsert_user_and_connection(
        self,
        *,
        email: str,
        display_name: str,
        refresh_token_enc: str,
        scopes: list[str],
        channel: str = "gmail",
    ) -> tuple[str, str]:
        from db.session import create_db_session

        User, ChannelAccount = _models()
        with create_db_session() as session:
            user = session.query(User).filter(User.email == email).one_or_none()
            if user is None:
                user = User(
                    id=str(uuid4()),
                    email=email,
                    display_name=display_name,
                    created_at=_now(),
                )
                session.add(user)
                session.flush()

            account = (
                session.query(ChannelAccount)
                .filter(
                    ChannelAccount.user_id == user.id,
                    ChannelAccount.channel == channel,
                    ChannelAccount.account_email == email,
                )
                .one_or_none()
            )
            if account is None:
                account = ChannelAccount(
                    id=str(uuid4()),
                    user_id=user.id,
                    channel=channel,
                    account_email=email,
                    connected_at=_now(),
                )
                session.add(account)
            account.refresh_token_enc = refresh_token_enc
            account.scopes = list(scopes)
            account.status = "connected"
            session.flush()

            # A new user is seeded with the six default categories (idempotent —
            # reconnecting or a user with an edited taxonomy adds nothing).
            from db.seed import ensure_default_taxonomy

            ensure_default_taxonomy(session, user.id)
            return user.id, account.id

    def load_refresh_token(self, *, user_id: str, connection_id: str) -> str:
        """Decrypt the stored refresh token for one user's connection.

        Scoped by `user_id` — a connection belonging to another user is not found.
        """
        from db.session import create_db_session

        _User, ChannelAccount = _models()
        with create_db_session() as session:
            account = (
                session.query(ChannelAccount)
                .filter(
                    ChannelAccount.id == connection_id,
                    ChannelAccount.user_id == user_id,
                )
                .one_or_none()
            )
            if account is None:
                raise ChannelError("no such mailbox connection for this user")
            if not account.refresh_token_enc:
                raise ReauthRequired("no stored refresh token — reconnect Gmail")
            return self._cipher.decrypt(account.refresh_token_enc)


def adapter_for_connection(*, user_id: str, connection_id: str, **kwargs):
    """Build a ready-to-use `GmailAdapter` for one user's connected mailbox."""
    from channels.gmail.adapter import GmailAdapter

    refresh_token = SqlConnectionStore().load_refresh_token(
        user_id=user_id, connection_id=connection_id
    )
    return GmailAdapter.for_refresh_token(refresh_token, user_id=user_id, **kwargs)
