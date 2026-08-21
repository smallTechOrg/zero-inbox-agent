"""Persistence of the Gmail connection: ``users`` + ``gmail_accounts``.

spec/data.md: users 1—1 gmail_accounts. The refresh token is Fernet-encrypted
*before* it reaches the database and is never returned, logged, or included in
an error message. Disconnect deletes the row; a failed refresh flips ``status``
to ``needs_reconnect``; reconnecting overwrites the token and restores
``connected``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from channels.base import ReauthRequired
from security.crypto import TokenCipher


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlConnectionStore:
    """All reads/writes of the encrypted per-user Gmail connection."""

    def __init__(self, cipher: TokenCipher | None = None) -> None:
        self._cipher = cipher or TokenCipher()

    def encrypt(self, refresh_token: str) -> str:
        return self._cipher.encrypt(refresh_token)

    def upsert_user_and_connection(
        self,
        *,
        email: str,
        name: str = "",
        picture_url: str = "",
        refresh_token_encrypted: str,
    ) -> tuple[str, str]:
        """One consent → user row + gmail_accounts row, idempotently.

        Reconnecting overwrites the stored token and restores ``connected``.
        A new user is seeded with the default taxonomy (idempotent).
        """
        from db.models import GmailAccount, User
        from db.session import create_db_session

        with create_db_session() as session:
            user = session.query(User).filter(User.email == email).one_or_none()
            if user is None:
                user = User(
                    id=str(uuid4()),
                    email=email,
                    name=name or email.split("@")[0],
                    picture_url=picture_url or None,
                    created_at=_now(),
                )
                session.add(user)
                session.flush()
            else:
                if name and not user.name:
                    user.name = name
                if picture_url and not user.picture_url:
                    user.picture_url = picture_url

            account = (
                session.query(GmailAccount)
                .filter(GmailAccount.user_id == user.id)
                .one_or_none()
            )
            if account is None:
                account = GmailAccount(
                    id=str(uuid4()),
                    user_id=user.id,
                    google_email=email,
                    refresh_token_encrypted=refresh_token_encrypted,
                    connected_at=_now(),
                )
                session.add(account)
            account.google_email = email
            account.refresh_token_encrypted = refresh_token_encrypted
            account.status = "connected"
            session.flush()

            from db.seed import ensure_default_taxonomy

            ensure_default_taxonomy(session, user.id)
            return user.id, account.id

    def load_refresh_token(self, *, user_id: str) -> str:
        """Decrypt the stored refresh token — scoped to one user, or reauth."""
        from db.models import GmailAccount
        from db.session import create_db_session

        with create_db_session() as session:
            account = (
                session.query(GmailAccount)
                .filter(GmailAccount.user_id == user_id)
                .one_or_none()
            )
            if account is None or not account.refresh_token_encrypted:
                raise ReauthRequired("no stored refresh token — reconnect Gmail")
            return self._cipher.decrypt(account.refresh_token_encrypted)

    def connection_for(self, user_id: str) -> dict | None:
        """``{"id", "google_email", "status", "connected_at"}`` or None.

        Never the token, in any form.
        """
        from db.models import GmailAccount
        from db.session import create_db_session

        with create_db_session() as session:
            account = (
                session.query(GmailAccount)
                .filter(GmailAccount.user_id == user_id)
                .one_or_none()
            )
            if account is None:
                return None
            return {
                "id": account.id,
                "google_email": account.google_email,
                "status": account.status,
                "connected_at": account.connected_at,
            }

    def mark_needs_reconnect(self, user_id: str) -> bool:
        """Flip the connection to ``needs_reconnect`` (revoked/expired token).

        Every surface that catches ``ReauthRequired`` calls this so the
        dashboard's reconnect banner is consistent app-wide. Idempotent.
        """
        from db.models import GmailAccount
        from db.session import create_db_session

        with create_db_session() as session:
            account = (
                session.query(GmailAccount)
                .filter(GmailAccount.user_id == user_id)
                .one_or_none()
            )
            if account is None:
                return False
            account.status = "needs_reconnect"
            return True

    def disconnect(self, user_id: str) -> str | None:
        """Delete the token row (spec: disconnect deletes the row).

        Returns the decrypted refresh token so the caller can best-effort revoke
        it at Google — the local ciphertext is deleted regardless.
        """
        from db.models import GmailAccount
        from db.session import create_db_session

        with create_db_session() as session:
            account = (
                session.query(GmailAccount)
                .filter(GmailAccount.user_id == user_id)
                .one_or_none()
            )
            if account is None:
                return None
            plaintext: str | None = None
            if account.refresh_token_encrypted:
                try:
                    plaintext = self._cipher.decrypt(account.refresh_token_encrypted)
                except Exception:  # noqa: BLE001 — an undecryptable token still gets deleted
                    plaintext = None
            session.delete(account)
            return plaintext


def adapter_for_user(*, user_id: str, **kwargs):
    """Build a ready-to-use :class:`GmailAdapter` for one user's mailbox.

    Raises ``ReauthRequired`` when no valid token is stored — callers map it to
    the structured ``gmail_reconnect`` error.
    """
    from channels.gmail.adapter import GmailAdapter

    refresh_token = SqlConnectionStore().load_refresh_token(user_id=user_id)
    return GmailAdapter.for_refresh_token(refresh_token, user_id=user_id, **kwargs)
