"""Fake Gmail service for the auth-gmail slice's tests.

A plain-Python chainable stand-in for a built ``googleapiclient`` service. It is
deliberately NOT from the ``googleapiclient`` package, so the suite-wide
``assert_not_real_gmail_request`` guard lets it through while still refusing any
real request object.
"""

from __future__ import annotations

from collections.abc import Callable


class FakeRequest:
    def __init__(self, fn: Callable[[], dict]):
        self._fn = fn

    def execute(self) -> dict:
        return self._fn()


class FakeGmailService:
    """Records every call as ``(method, kwargs)`` on ``self.calls``.

    ``threads_data`` maps thread id → the raw ``threads().get`` payload;
    ``thread_order`` is the newest-first listing order.
    ``estimates`` is an optional ``query -> int`` callable used for
    ``resultSizeEstimate`` (defaults to the number of known threads).
    """

    def __init__(
        self,
        threads_data: dict[str, dict] | None = None,
        *,
        estimates: Callable[[str], int] | None = None,
        profile_email: str = "user@example.com",
    ):
        self.calls: list[tuple[str, dict]] = []
        self.threads_data = threads_data or {}
        self.thread_order = list(self.threads_data)
        self._estimates = estimates
        self._profile_email = profile_email

    # chain roots -------------------------------------------------------
    def users(self):
        return self

    def threads(self):
        return self

    def labels(self):
        return self

    # reads -------------------------------------------------------------
    def getProfile(self, userId):
        self.calls.append(("getProfile", {"userId": userId}))
        return FakeRequest(lambda: {"emailAddress": self._profile_email})

    def list(self, **kwargs):
        self.calls.append(("threads.list", kwargs))

        def run():
            query = kwargs.get("q", "")
            estimate = (
                self._estimates(query) if self._estimates else len(self.thread_order)
            )
            limit = kwargs.get("maxResults") or len(self.thread_order)
            return {
                "threads": [{"id": tid} for tid in self.thread_order[:limit]],
                "resultSizeEstimate": estimate,
            }

        return FakeRequest(run)

    def get(self, **kwargs):
        self.calls.append(("threads.get", kwargs))
        return FakeRequest(lambda: dict(self.threads_data[kwargs["id"]]))

    # the one write the real system performs ----------------------------
    def modify(self, **kwargs):
        self.calls.append(("threads.modify", kwargs))
        return FakeRequest(
            lambda: {"id": kwargs["id"], "labelIds": ["INBOX", "Label_1"]}
        )

    # helpers ------------------------------------------------------------
    def calls_named(self, name: str) -> list[dict]:
        return [kw for method, kw in self.calls if method == name]


def make_thread(
    thread_id: str,
    *,
    sender: str = "GitHub <notifications@github.com>",
    subject: str = "A subject",
    snippet: str = "the ninety char gmail snippet",
    internal_ms: int = 1_700_000_000_000,
    labels: list[str] | None = None,
    list_unsubscribe: str | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "To", "value": "user@example.com"},
    ]
    if list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": list_unsubscribe})
    return {
        "id": thread_id,
        "messages": [
            {
                "id": f"{thread_id}-m1",
                "internalDate": str(internal_ms),
                "snippet": snippet,
                "labelIds": labels or ["INBOX", "UNREAD"],
                "payload": {"headers": headers},
            }
        ],
    }
