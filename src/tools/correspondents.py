"""Truth about who a correspondent actually is.

Two facts the rest of the system needs, and neither of them requires an LLM:

* **A ``no-reply`` sender can never be a genuine correspondent.** Replying to
  ``no_reply@email.apple.com`` is not a relationship, so a reply-history signal
  derived from one is noise — and, worse, a never-miss hold that never clears.
* **The user is not his own correspondent.** Harvesting ``To``/``Cc`` from the
  user's own ``SENT`` mail makes the user his own most-replied-to address (the
  live signature was *"24 replies of 0 received"*), which held 18 threads.

Pure functions, no I/O. See spec/capabilities/never-miss-safeguards.md.
"""

from __future__ import annotations

#: The raw spellings a no-reply local part is written with in the wild. Matching
#: is case-, dot-, hyphen- and underscore-insensitive, so every one of these
#: collapses to the same needle — the tuple is the documented, testable surface.
NO_REPLY_PATTERNS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
)

_SEPARATORS = str.maketrans({".": "", "-": "", "_": "", " ": ""})

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def _split(email: str) -> tuple[str, str]:
    """``("local", "domain")``, lowercased and stripped. Missing parts are ``""``."""
    address = (email or "").strip().lower()
    # Tolerate a display form: "Name <a@b.com>".
    if "<" in address and address.endswith(">"):
        address = address[address.rindex("<") + 1 : -1].strip()
    if "@" not in address:
        return address, ""
    local, _, domain = address.rpartition("@")
    return local, domain


def _squash(value: str) -> str:
    """Fold the separators a sender may use to disguise a well-known local part."""
    return value.translate(_SEPARATORS)


#: Precomputed once — every pattern squashed to its separator-free form.
_NO_REPLY_NEEDLES: tuple[str, ...] = tuple(
    dict.fromkeys(_squash(pattern) for pattern in NO_REPLY_PATTERNS)
)


def is_no_reply(email: str) -> bool:
    """True when the local part names an unattended, machine-only mailbox.

    Matching is on the **local part only** — ``noreply@x.com`` matches, and a
    person at ``reply@x.com`` or ``replies@x.com`` does not. Separators are
    folded, so ``no-reply``, ``no_reply``, ``No.Reply`` and ``noreply`` are one
    thing. ``noreplyneeded@example.com`` matches **by design**: a substring rule
    is the conservative choice here, because the cost of a false positive is a
    lost never-miss *signal* (the thread is still classified normally) while the
    cost of a false negative is a thread held in the inbox forever.
    """
    local, _domain = _split(email)
    if not local:
        return False
    squashed = _squash(local)
    return any(needle in squashed for needle in _NO_REPLY_NEEDLES)


def normalize_address(email: str) -> str:
    """Canonical comparison form: lowercased, ``+tag`` dropped, Gmail dots dropped.

    ``+tag`` stripping is universal (it is an addressing convention, not a
    Gmail one); dot-insensitivity is applied only for the domains where it is
    actually true, so ``first.last@company.com`` stays distinct from
    ``firstlast@company.com``.
    """
    local, domain = _split(email)
    if not local:
        return ""
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if domain else local


def is_self_address(email: str, *, account_email: str, aliases: list[str]) -> bool:
    """True when ``email`` is the account itself or one of its ``sendAs`` aliases.

    Compared in :func:`normalize_address` form, so ``psykrsna@gmail.com``,
    ``psy.krsna@gmail.com`` and ``psykrsna+news@gmail.com`` are all the same
    person — the exact three spellings that produced the live defect.
    """
    candidate = normalize_address(email)
    if not candidate:
        return False
    known = {normalize_address(account_email)}
    known.update(normalize_address(alias) for alias in (aliases or []))
    known.discard("")
    return candidate in known
