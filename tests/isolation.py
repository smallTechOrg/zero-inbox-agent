"""Structural isolation guard for the test suite.

Root cause of the ``e2e-actions-test`` pollution (Phase 9, slice 8): a test was
*able* to bind to a real user's account and commit a ``Category`` row onto it.
The same defect class as the cached-``get_settings()`` incident. Deleting the row
is the symptom; this module makes the write impossible and the failure loud.

Two independent layers, both plain assertions with no configuration surface:

* :func:`assert_isolated_db` — the bound engine must point at a throwaway file
  under pytest's ``tmp_path``. ``zero_inbox.db`` and anything under ``data/`` is
  refused outright.
* :func:`assert_test_user` / :func:`assert_test_mailbox` — test data must carry
  the reserved ``test-`` id prefix and an ``@example.com`` / ``@test.invalid``
  address. The measured real id and address are a hard deny-list.

Neither layer can be disabled by a marker, an env var or a fixture override. A
test that genuinely needs a real account is a spec question, not a local
override.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

__all__ = [
    "IsolationError",
    "RealDatabaseError",
    "RealUserError",
    "RealGmailError",
    "TEST_USER_PREFIX",
    "ALLOWED_TEST_EMAIL_DOMAINS",
    "DENIED_USER_ID_PREFIXES",
    "DENIED_EMAILS",
    "GUARDED_TABLES",
    "is_test_user_id",
    "looks_like_real_account_id",
    "normalise_gmail",
    "is_denied_email",
    "assert_isolated_db",
    "assert_test_user",
    "assert_test_mailbox",
    "assert_row_not_real",
    "assert_not_real_gmail_request",
    "assert_not_real_gmail_write",
    "GMAIL_URI_FRAGMENTS",
    "current_test_name",
]


class IsolationError(AssertionError):
    """Base: the test suite tried to touch production state."""


class RealDatabaseError(IsolationError):
    """The session/engine is bound to a real database file."""


class RealUserError(IsolationError):
    """A row was about to be written against a non-test (real) account."""


class RealGmailError(IsolationError):
    """A test was about to issue a real Gmail API call."""


#: Reserved prefix that marks a synthetic account. Everything the suite creates
#: must use it — it is the single token that separates test data from real data.
TEST_USER_PREFIX = "test-"

#: The only mailbox domains a test may use. Both are non-routable by design
#: (RFC 2606 / RFC 6761), so a test can never mail a real person either.
ALLOWED_TEST_EMAIL_DOMAINS = ("example.com", "test.invalid")

#: Hard deny-list — the measured real account. Prefix match, because the id is
#: recorded truncated in the spec and a UUID prefix is already unique.
DENIED_USER_ID_PREFIXES = ("6b4ab0f4",)

#: Hard deny-list — the user's real mailbox.
DENIED_EMAILS = ("psykrsna@gmail.com",)

#: Real database files that must never be bound inside a test process.
#: ``agent.db`` is the redesigned schema's production file (spec/architecture.md:
#: ``sqlite:///./data/agent.db``); ``zero_inbox.db`` is the old design's — both
#: stay denied so a stale checkout can't leak either.
DENIED_DB_FILENAMES = ("zero_inbox.db", "agent.db")

#: The user-scoped tables the commit guard watches (spec/data.md). These are the
#: tables whose rows are visible in the product surface (taxonomy chips, run
#: cards, ledger, Gmail connection) — a leaked row here is a leaked row the user
#: sees, and a leaked ``mutations`` row means a test described a Gmail write
#: against a real mailbox.
GUARDED_TABLES = (
    "categories",
    "gmail_accounts",
    "runs",
    "thread_decisions",
    "mutations",
)


#: The columns on a guarded row that can carry a mailbox address.
EMAIL_COLUMNS = ("google_email", "account_email", "email", "from_email", "sender", "sender_email")

#: A real account id in this system is a uuid4 (``db.models._uuid``). A test id
#: that has that shape is a copied production id — refused even if it is not on
#: the deny-list, because the deny-list only knows the ids we already found.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def current_test_name() -> str:
    """The running test's node id, so a guard failure names the culprit."""
    return os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>").split(" (")[0]


def is_test_user_id(user_id: object) -> bool:
    """True only for the reserved synthetic-account form."""
    if not isinstance(user_id, str) or not user_id.startswith(TEST_USER_PREFIX):
        return False
    # A uuid4 smuggled in behind the reserved prefix is a copied production id,
    # not synthetic data.
    if _UUID_RE.match(user_id[len(TEST_USER_PREFIX) :]):
        return False
    return not _is_denied_user_id(user_id)


def looks_like_real_account_id(user_id: object) -> bool:
    """True if the id has the shape this system mints for a REAL account.

    ``db.models._uuid`` produces uuid4s, so a bare uuid4 — or one smuggled in
    behind the reserved prefix — is a copied production id, not synthetic data.
    The deny-list only knows the ids we have already found; this closes the ones
    we have not.
    """
    if not isinstance(user_id, str):
        return False
    return bool(_UUID_RE.match(_strip_test_prefix(user_id)))


def normalise_gmail(email: object) -> str:
    """Canonical form of a Gmail address for deny-list comparison.

    Gmail ignores dots and everything after ``+`` in the local part, so
    ``psy.krsna+zero@gmail.com`` is the user's real mailbox. Comparing raw
    strings would let that through.
    """
    if not isinstance(email, str) or "@" not in email:
        return ""
    local, _, domain = email.strip().lower().rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def is_denied_email(email: object) -> bool:
    """True for the user's real mailbox in any of its Gmail spellings."""
    normalised = normalise_gmail(email)
    return bool(normalised) and normalised in {normalise_gmail(e) for e in DENIED_EMAILS}


def _strip_test_prefix(user_id: str) -> str:
    """A real id cannot be laundered by bolting ``test-`` onto the front."""
    return user_id[len(TEST_USER_PREFIX) :] if user_id.startswith(TEST_USER_PREFIX) else user_id


def _is_denied_user_id(user_id: str) -> bool:
    lowered = _strip_test_prefix(user_id).lower()
    return any(lowered.startswith(p.lower()) for p in DENIED_USER_ID_PREFIXES)


def _tmp_roots() -> tuple[Path, ...]:
    """Directories a throwaway test DB is allowed to live in.

    pytest's ``tmp_path`` lives under the platform temp dir; on macOS that is
    ``/var/folders/...`` which resolves to ``/private/var/folders/...``. Both
    spellings are accepted so the guard does not fire on a symlink detail.
    """
    tmp = Path(tempfile.gettempdir())
    roots = {tmp, tmp.resolve()}
    override = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    if override:
        roots.update({Path(override), Path(override).resolve()})
    return tuple(roots)


def _db_path(url: str) -> Path | None:
    """The filesystem path a SQLite URL points at, or None for in-memory."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw).expanduser()


def assert_isolated_db(engine) -> None:
    """Refuse any engine not bound to a throwaway file under ``tmp_path``.

    Raises :class:`RealDatabaseError` naming the offending URL. In-memory SQLite
    is refused too: it is not what the fixture builds, so seeing it means the
    binding did not take effect.
    """
    url = str(getattr(engine, "url", engine))
    path = _db_path(url)
    if path is None:
        raise RealDatabaseError(
            f"{current_test_name()}: engine URL {url!r} is not a file-backed "
            "SQLite database under tmp_path. Tests must bind the _isolated_db "
            "fixture's throwaway file — never an in-memory or production URL."
        )

    if path.name in DENIED_DB_FILENAMES:
        raise RealDatabaseError(
            f"{current_test_name()}: refusing to bind the REAL database "
            f"{url!r}. zero_inbox.db holds live user data; tests run against "
            "the _isolated_db tmp_path file only."
        )

    resolved = _resolve(path)
    if "data" in resolved.parts:
        raise RealDatabaseError(
            f"{current_test_name()}: refusing to bind {url!r} — it resolves to "
            f"{resolved}, which is under a data/ directory. Production and "
            "migration-scratch databases live there; tests use tmp_path."
        )

    if not any(_is_relative_to(resolved, root) for root in _tmp_roots()):
        raise RealDatabaseError(
            f"{current_test_name()}: refusing to bind {url!r} — it resolves to "
            f"{resolved}, which is not under the pytest tmp_path root "
            f"({', '.join(str(r) for r in _tmp_roots())})."
        )


def _resolve(path: Path) -> Path:
    if not path.is_absolute():
        path = Path.cwd() / path
    # ``resolve()`` on a not-yet-created file still normalises the parents.
    return Path(os.path.normpath(str(path.resolve())))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def assert_test_user(user_id: object, *, table: str = "row") -> None:
    """The row's ``user_id`` must be a reserved synthetic id.

    Raises :class:`RealUserError` naming the test and the row it tried to write.
    """
    if isinstance(user_id, str) and _is_denied_user_id(user_id):
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table} row for "
            f"user_id={user_id!r} — that is the USER'S REAL ACCOUNT "
            "(explicit deny-list). This is exactly the write that left "
            "'e2e-actions-test' on the live taxonomy. Use a "
            f"{TEST_USER_PREFIX!r}-prefixed synthetic id."
        )
    if not is_test_user_id(user_id):
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table} row for "
            f"user_id={user_id!r} — test data must use the reserved "
            f"{TEST_USER_PREFIX!r} id prefix (e.g. 'test-user-alice'). Any other "
            "id may belong to a real account. See tests/isolation.py."
        )


def assert_test_mailbox(email: object, *, table: str = "row") -> None:
    """The mailbox address must be non-routable test-only."""
    if is_denied_email(email):
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table} row for "
            f"account_email={email!r} — that is the USER'S REAL MAILBOX "
            "(deny-list; dots and +tags are normalised)."
        )
    if not isinstance(email, str) or "@" not in email:
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table} row with "
            f"account_email={email!r} — not an address. Test mailboxes must end "
            f"in {' or '.join('@' + d for d in ALLOWED_TEST_EMAIL_DOMAINS)}."
        )
    domain = email.rsplit("@", 1)[1].strip().lower()
    if domain not in ALLOWED_TEST_EMAIL_DOMAINS:
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table} row for "
            f"account_email={email!r} — test mailboxes must end in "
            f"{' or '.join('@' + d for d in ALLOWED_TEST_EMAIL_DOMAINS)}, so a "
            "test can never bind (or mail) a real inbox."
        )


# --- the write-time guard (what the autouse fixture enforces) --------------
#
# ``assert_test_user`` / ``assert_test_mailbox`` above are the STRICT convention
# for new fixtures: reserved ``test-`` ids and non-routable domains. The autouse
# commit guard cannot apply them verbatim to every row — the pre-Phase-9 suite
# writes ~100 rows under legacy synthetic ids (``u1``, ``user-alice``) and seven
# slices are being generated against those fixtures right now. So the commit
# guard enforces the part that actually closes the escape, with no opt-out:
#
#   1. the session must be bound to a throwaway tmp_path database
#      (``assert_isolated_db``) — the real account's rows are then simply not
#      reachable from a test process at all;
#   2. a deny-listed real id or real mailbox raises, whatever the DB;
#   3. a ``user_id`` shaped like a real account id (uuid4) raises — a production
#      id pasted into a fixture is refused even though the deny-list has never
#      seen it. The ``test-`` prefix is stripped before both checks, so a real
#      id cannot be laundered as ``test-6b4ab0f4-…``.
#
# See the slice report: the "every user_id must start with test-" wording in
# spec/roadmap.md is recorded as a spec conflict, not silently dropped.


def assert_row_not_real(instance, *, table: str) -> None:
    """Refuse a guarded row that names a real account. Raises :class:`RealUserError`."""
    user_id = getattr(instance, "user_id", None)
    if isinstance(user_id, str) and _is_denied_user_id(user_id):
        raise RealUserError(
            f"{current_test_name()}: refusing to write a {table!r} row for "
            f"user_id={user_id!r} — that is the USER'S REAL ACCOUNT (deny-list). "
            "This is exactly the write that left 'e2e-actions-test' on the live "
            f"taxonomy. Use a {TEST_USER_PREFIX!r}-prefixed synthetic id."
        )
    # NB: uuid4 *shape* is deliberately NOT refused here. Tests legitimately
    # create a User row inside the isolated database and reuse its generated id
    # (``db.models._uuid``), so shape alone cannot distinguish a synthetic id
    # from a production one. ``assert_test_user`` — the strict convention for new
    # fixtures — does refuse it. What makes this safe is the binding check above:
    # an id only reaches a real account if the session is bound to the real DB,
    # and that is refused outright.
    for column in EMAIL_COLUMNS:
        value = getattr(instance, column, None)
        if is_denied_email(value):
            raise RealUserError(
                f"{current_test_name()}: refusing to write a {table!r} row with "
                f"{column}={value!r} — that is the USER'S REAL MAILBOX "
                "(deny-list; dots and +tags are normalised)."
            )


#: Gmail API host fragments — a request whose URI names either is a Gmail call.
GMAIL_URI_FRAGMENTS = ("gmail.googleapis.com", "/gmail/v1/")


def assert_not_real_gmail_write(method: object, uri: object) -> None:
    """Refuse any non-GET request to the Gmail API from inside a test.

    The redesign's discipline (spec/architecture.md § Test-Isolation Guard):
    Gmail READS stay real, Gmail WRITES never leave the process. Every Gmail
    mutation (``threads.modify``, ``labels.create`` …) is a POST/PATCH/DELETE,
    and every read the audit or triage path needs is a GET — so blocking
    non-GET Gmail traffic sandbox-proofs the live mailbox without faking reads.
    OAuth token refresh (``oauth2.googleapis.com``) is untouched.
    """
    uri_s = uri if isinstance(uri, str) else ""
    method_s = (method if isinstance(method, str) else "GET").upper()
    if method_s != "GET" and any(f in uri_s for f in GMAIL_URI_FRAGMENTS):
        raise RealGmailError(
            f"{current_test_name()}: refusing a live Gmail WRITE from a test "
            f"({method_s} {uri_s}). Under tests the mutation choke point must "
            "record the audit row and return applied WITHOUT calling Gmail "
            "(AGENT_TEST_ISOLATION=1) — a real mutation on the live mailbox "
            "cannot be undone by a test teardown."
        )


def assert_not_real_gmail_request(request) -> None:
    """Refuse to execute a live googleapiclient request from inside a test.

    A test drives Gmail through a fake service. A request object whose class
    comes from ``googleapiclient`` is a real, authenticated call about to hit the
    user's mailbox — the Gmail-side twin of binding ``zero_inbox.db``.
    """
    module = type(request).__module__ or ""
    if module.split(".")[0] == "googleapiclient":
        raise RealGmailError(
            f"{current_test_name()}: refusing to execute a real Gmail API request "
            f"({type(request).__module__}.{type(request).__qualname__}). Tests "
            "mutate a fake Gmail service only — a live mutation touches the "
            "user's actual mail and cannot be undone by a test teardown."
        )
