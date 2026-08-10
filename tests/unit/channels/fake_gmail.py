"""A thin in-memory stand-in for the googleapiclient Gmail service.

Not a mock: it is a real (tiny) implementation of the same call shape, so it
composes and survives refactors of the adapter internals.
"""

from __future__ import annotations

import json

from googleapiclient.errors import HttpError
from httplib2 import Response


def http_error(status: int, reason: str = "error") -> HttpError:
    resp = Response({"status": status, "reason": reason})
    resp.status = status
    return HttpError(resp, json.dumps({"error": {"message": reason}}).encode())


class _Request:
    def __init__(self, result, errors: list[HttpError] | None = None):
        self._result = result
        self._errors = list(errors or [])

    def execute(self, **_kwargs):
        if callable(self._result):
            return self._result()
        if self._errors:
            raise self._errors.pop(0)
        return self._result


class _Threads:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def list(self, *, userId, labelIds=None, maxResults=None, pageToken=None, q=None):
        self._s.thread_list_calls.append(
            {"labelIds": labelIds, "maxResults": maxResults, "pageToken": pageToken, "q": q}
        )
        page = self._s.thread_pages[self._s._page_index]
        self._s._page_index = min(self._s._page_index + 1, len(self._s.thread_pages) - 1)
        return _Request(page)

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        self._s.thread_get_calls.append({"id": id, "format": format})
        if id in self._s.thread_errors:
            return _Request(None, errors=[self._s.thread_errors[id]])
        return _Request(self._s.threads[id])

    def modify(self, *, userId, id, body):
        call = {
            "id": id,
            "add": list(body.get("addLabelIds") or []),
            "remove": list(body.get("removeLabelIds") or []),
        }
        self._s.thread_modify_calls.append(call)
        current = set((self._s.threads.get(id) or {}).get("labelIds") or [])
        current.update(call["add"])
        current.difference_update(call["remove"])
        return _Request({"id": id, "labelIds": sorted(current)})


class _Messages:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def list(self, *, userId, labelIds=None, maxResults=None, pageToken=None, q=None):
        self._s.message_list_calls.append({"labelIds": labelIds, "q": q})
        return _Request({"messages": [{"id": m["id"]} for m in self._s.sent_messages]})

    def get(self, *, userId, id, format=None, metadataHeaders=None):
        found = next(m for m in self._s.sent_messages if m["id"] == id)
        return _Request(found)


class _Labels:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def list(self, *, userId):
        return _Request({"labels": self._s.labels})


class _Users:
    def __init__(self, service: "FakeGmailService"):
        self._s = service

    def threads(self):
        return _Threads(self._s)

    def messages(self):
        return _Messages(self._s)

    def labels(self):
        return _Labels(self._s)

    def getProfile(self, *, userId):
        def produce():
            # Errors are consumed per attempt, so a retry sees the next outcome.
            if self._s.profile_errors:
                raise self._s.profile_errors.pop(0)
            return {"emailAddress": self._s.account_email}

        return _Request(produce)


class FakeGmailService:
    def __init__(
        self,
        *,
        threads: dict | None = None,
        thread_pages: list[dict] | None = None,
        sent_messages: list[dict] | None = None,
        labels: list[dict] | None = None,
        account_email: str = "user@example.com",
    ):
        self.threads = threads or {}
        self.thread_pages = thread_pages or [{"threads": [{"id": t} for t in self.threads]}]
        self.sent_messages = sent_messages or []
        self.labels = labels or [{"id": "INBOX", "name": "INBOX"}]
        self.account_email = account_email
        self.thread_errors: dict[str, HttpError] = {}
        self.profile_errors: list[HttpError] = []
        self.thread_list_calls: list[dict] = []
        self.thread_get_calls: list[dict] = []
        self.thread_modify_calls: list[dict] = []
        self.message_list_calls: list[dict] = []
        self._page_index = 0

    def users(self):
        return _Users(self)
