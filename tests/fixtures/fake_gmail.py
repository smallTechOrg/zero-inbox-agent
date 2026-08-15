"""Fake Gmail READ layer for integration tests.

The isolation guard already sandboxes Gmail WRITES (choke-point flag + network
backstop). Reads, however, need real credentials the synthetic test user does
not have — so integration tests inject an in-memory inbox at the read seam.

CROSS-SLICE CONTRACT (auth-gmail / triage-graph slices): the thread-listing
entry point lives in ``src/channels/gmail/client.py`` under one of the callable
names probed below, takes the user and a limit, and returns the privacy-bounded
per-thread views (``ClassifierView`` or equivalent mappings). ``install_inbox``
re-points it; if no probe matches, the test FAILS LOUDLY naming the contract —
never a silent pass.
"""

from __future__ import annotations

import pytest

_LIST_FN_NAMES = (
    "list_inbox_threads",
    "fetch_inbox_threads",
    "get_inbox_threads",
    "list_threads",
    "fetch_threads",
)

_MODULE_NAMES = (
    "channels.gmail.client",
    "channels.gmail.adapter",
    "channels.gmail.normalize",
)


def _to_view(thread: dict):
    """Best-effort conversion of a fixture dict to the domain ClassifierView.

    Strips the never-allowed ``body`` key first — the sentinel must only be
    able to leak through a REAL privacy bug, not through this shim.
    """
    data = {k: v for k, v in thread.items() if k != "body"}
    # Field-name aliases between the fixture spelling and the domain spelling.
    aliases = {
        "gmail_thread_id": data.get("thread_id"),
        "list_unsubscribe_present": data.get("has_list_unsubscribe"),
        "thread_message_count": data.get("message_count"),
        "thread_size": data.get("message_count"),
        "sender_address": data.get("sender"),
        "sender_name": data.get("sender"),
    }
    data.update({k: v for k, v in aliases.items() if v is not None})
    try:
        from domain.triage import ClassifierView  # type: ignore
    except ImportError:
        try:
            from domain.item import ClassifierView  # type: ignore
        except ImportError:
            return data
    try:
        import inspect

        params = set(inspect.signature(ClassifierView).parameters)
        return ClassifierView(**{k: v for k, v in data.items() if k in params})
    except Exception:
        return data


def install_inbox(monkeypatch, threads: list[dict]) -> list:
    """Monkeypatch the Gmail thread-listing seam to serve ``threads``.

    Returns the served view objects. Newest-first order is the caller's list
    order. The patched callable respects a ``limit``/``max_results`` kwarg or
    second positional arg when present.
    """
    views = [_to_view(t) for t in threads]

    def fake_list(*args, **kwargs):
        limit = kwargs.get("limit") or kwargs.get("max_results") or kwargs.get("chunk_limit")
        if limit is None:
            for a in args[1:]:
                if isinstance(a, int):
                    limit = a
                    break
        return views[: int(limit)] if limit else list(views)

    import importlib

    for mod_name in _MODULE_NAMES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for fn_name in _LIST_FN_NAMES:
            if callable(getattr(mod, fn_name, None)):
                monkeypatch.setattr(mod, fn_name, fake_list)
                # Also re-point any module that imported it by value.
                _repoint_importers(monkeypatch, mod_name, fn_name, fake_list)
                return views
    pytest.fail(
        "fake-gmail contract: none of "
        f"{_LIST_FN_NAMES} found in {_MODULE_NAMES}. The Gmail read layer must "
        "expose its INBOX thread-listing entry point under one of these names "
        "so tests can serve a synthetic inbox (tests/fixtures/fake_gmail.py)."
    )


def _repoint_importers(monkeypatch, mod_name: str, fn_name: str, replacement) -> None:
    import sys

    for other in list(sys.modules.values()):
        if other is None or getattr(other, "__name__", "") == mod_name:
            continue
        target = getattr(other, fn_name, None)
        if target is not None and getattr(target, "__module__", None) == mod_name:
            try:
                monkeypatch.setattr(other, fn_name, replacement)
            except (AttributeError, TypeError):
                pass


def force_refresh_failure(monkeypatch) -> None:
    """Make Google credential refresh raise invalid_grant for every user.

    Used to assert the revoked-token path yields ``gmail_reconnect``, never a
    traceback. Fails loudly if the oauth module exposes no known entry point.
    """
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:  # pragma: no cover
        RefreshError = RuntimeError  # type: ignore

    def boom(*_args, **_kwargs):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    import importlib

    try:
        oauth = importlib.import_module("channels.gmail.oauth")
    except ImportError:
        pytest.fail("fake-gmail contract: channels/gmail/oauth.py must exist.")
    for fn_name in (
        "get_credentials",
        "credentials_for_user",
        "load_credentials",
        "refresh_credentials",
        "credentials",
    ):
        if callable(getattr(oauth, fn_name, None)):
            monkeypatch.setattr(oauth, fn_name, boom)
            _repoint_importers(monkeypatch, "channels.gmail.oauth", fn_name, boom)
            return
    pytest.fail(
        "fake-gmail contract: channels/gmail/oauth.py must expose its "
        "credentials loader (get_credentials(user_id) or equivalent) so tests "
        "can simulate a revoked token."
    )
