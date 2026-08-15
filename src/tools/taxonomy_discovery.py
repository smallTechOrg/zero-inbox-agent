"""Derive the taxonomy from the user's ACTUAL mail, and turn concentration into
deterministic tier-1 rules.

spec/capabilities/inbox-derived-taxonomy.md is the source of truth.

The defect this closes, measured on the live account: the default six categories
are a reasonable guess about a *generic* inbox and a poor description of *this*
one, which is ~1,586 Facebook threads, ~635 BookMyShow, 334 Jagriti Theatre, ~176
Apple, 78 PayPal — **all of it collapsing into "Notifications"**. That is exactly
why 16 threads found no fitting category (`needs_your_call`) and 46 landed under
the autonomy bar (`below_threshold`): the model was being asked to squeeze
concentrated automated mail into an ill-fitting generic bucket, which is what
produces middling confidence.

Four steps, in order, and the ordering is the design:

1. :func:`build_census` — **deterministic, free, no LLM.** Aggregate every
   ingested thread by sender, domain and ``List-Id`` and attach the free signals.
   The census decides what *exists*.
2. :func:`propose_taxonomy` — the LLM **names and groups** what the census found.
   It cannot invent a category with no evidence behind it, and on total model
   failure this returns the deterministic proposal marked ``partial`` — **never**
   the seed six presented as derived.
3. :func:`mine_sender_rules` — **the load-bearing step.** Every sender / domain /
   ``List-Id`` clearing the concentration bar becomes an ordinary ``Rule`` row.
4. :func:`materialise_rules` — writes them, idempotently.

**There is no second classification path here.** The rules minted in step 3 are
plain rows consumed by the *unchanged* ``tools.rules.apply_rules`` matcher in the
*unchanged* ``graph.nodes.apply_deterministic_rules`` node. This module writes no
classifier, adds no graph node and calls no LLM at triage time. That is what
makes the phase's success measure reachable rather than aspirational: roughly
half the mailbox stops being asked of the model at all, lands ``decided_by="rule"``
at ``0.95``, and the run's own ``counts.by_tier`` is the proof it happened. A
parallel classifier would be plumbing that is never wired, and would also destroy
that proof.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.correspondents import is_no_reply
from tools.rules import (
    MINED_MATCHER_CLAUSES,
    MINED_RULE_CONFIDENCE,
    VALID_ACTIONS,
    mined_rule_action,
    mined_rule_name,
    normalise_matcher,
)
from tools.taxonomy import NEVER_ARCHIVE_KEYS, TaxonomyError, validate_default_action

__all__ = [
    "CONCENTRATION_MIN_THREADS",
    "MAX_SENDERS_PER_CALL",
    "GAP_SUBJECT_MAX_CHARS",
    "GAP_STATUSES",
    "DiscoveryError",
    "build_census",
    "build_gap_set",
    "propose_taxonomy",
    "validate_proposal",
    "mine_sender_rules",
    "materialise_rules",
]

#: The concentration bar for minting a deterministic tier-1 rule: a sender /
#: domain / ``List-Id`` must account for **at least this many threads** and land
#: in a single dominant category.
#:
#: Ten is deliberately the SAME number the discovery success criterion uses for
#: "every sender contributing >= 10 threads must be named in some category's
#: evidence list", so the two cannot drift apart: everything big enough to be
#: worth naming is big enough to be worth deciding deterministically. It is a
#: module constant with no ``user_settings`` knob — a user-tunable bar here would
#: silently change how much of the mailbox the LLM is asked about, and therefore
#: what the run costs, with no way to see why.
CONCENTRATION_MIN_THREADS = 10

#: Census rows per LLM call. The census of a large mailbox is thousands of rows;
#: sending it whole would blow the context window and silently truncate — which
#: would drop exactly the long-tail senders discovery exists to notice.
MAX_SENDERS_PER_CALL = 200

#: Gap-set subjects are truncated to the same 60 characters the live feed already
#: uses. The privacy invariant is absolute: no bodies, and the census aggregates
#: carry counts and addresses only — no subjects at all.
GAP_SUBJECT_MAX_CHARS = 60

#: The two decision states that ARE the feedback signal. ``needs_your_call`` is
#: the taxonomy telling you where its hole is; ``below_threshold`` is largely the
#: model hedging on mail it was forced into an ill-fitting category. Driving both
#: to zero is the measure of a good taxonomy.
GAP_STATUSES: tuple[str, ...] = ("needs_your_call", "below_threshold")


def _is_gap(Decision):  # noqa: N803 - the ORM class, not an instance
    """The gap-set predicate, as ONE SQLAlchemy expression used by every query.

    Both columns must be checked, and getting this wrong is silent. ``status``
    carries ``needs_your_call``; ``below_threshold`` **only ever appears in
    ``autonomy_state``** (``status`` stays ``proposed`` for those rows). A
    ``status``-only filter therefore finds the 16 and misses all 46 — discovery
    would report a smaller hole than the taxonomy actually has, and the model
    would never be told about the mail it hedged on. That is the difference
    between closing the gap and appearing to.
    """
    from sqlalchemy import or_

    return or_(
        Decision.status.in_(GAP_STATUSES),
        Decision.autonomy_state.in_(GAP_STATUSES),
    )

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "taxonomy_discovery.md"

_SYSTEM = (
    "You derive an email taxonomy from one person's real sender census. "
    "You never invent a sender. You respond with a JSON array and nothing else."
)


class DiscoveryError(ValueError):
    """A discovery input or proposal is structurally invalid."""


def _models():
    from db import models

    return models


# --------------------------------------------------------------- 1. the census


def _domain_of(email: str) -> str:
    return (email or "").strip().lower().rpartition("@")[2]


def build_census(session: Session, *, user_id: str) -> list[dict]:
    """Aggregate every ingested thread by sender, domain and ``List-Id``.

    Deterministic and free — no LLM, one pass over ``items`` plus a lookup of
    ``sender_profiles``. Returns rows shaped::

        {kind, value, from_email, from_domain, list_id, thread_count,
         unread_count, ever_replied, is_no_reply, has_unsubscribe, in_gap_set}

    ``kind`` is ``sender`` | ``domain`` | ``list_id``. Rows are ordered by
    ``thread_count`` descending, so a caller that pages the census sends the
    concentration first — the part that actually earns a category.

    **No subjects.** The aggregates carry counts and addresses only; that is the
    privacy invariant, not an oversight.
    """
    models = _models()
    Item = models.Item

    # Deliberately a Python-side fold rather than three GROUP BY queries: the
    # per-sender, per-domain and per-List-Id aggregates all come from the same
    # single scan, they must agree with each other exactly (a domain's
    # thread_count is the sum of its senders'), and the boolean folds
    # (``is_unread``, ``unsubscribe_url IS NOT NULL``) are dialect-quirky in raw
    # SQL. One scan of a mailbox-sized table is cheap; two aggregates that
    # disagree are a bug the user would see as a wrong thread count.
    items = (
        session.execute(
            select(
                Item.from_email,
                Item.from_domain,
                Item.list_id,
                Item.is_unread,
                Item.unsubscribe_url,
            ).where(Item.user_id == user_id)
        )
        .tuples()
        .all()
    )

    replied = _replied_addresses(session, user_id=user_id)
    gap_senders = _gap_senders(session, user_id=user_id)

    buckets: dict[tuple[str, str], dict] = {}

    def bucket(kind: str, value: str) -> dict | None:
        value = (value or "").strip().lower()
        if not value:
            return None
        key = (kind, value)
        row = buckets.get(key)
        if row is None:
            row = {
                "kind": kind,
                "value": value,
                "from_email": value if kind == "sender" else None,
                "from_domain": (
                    value if kind == "domain" else (_domain_of(value) if kind == "sender" else None)
                ),
                "list_id": value if kind == "list_id" else None,
                "thread_count": 0,
                "unread_count": 0,
                "ever_replied": False,
                # ``is_no_reply`` is a first-class taxonomy signal: an address
                # that cannot receive a reply is structurally not a
                # correspondent. Free, zero-token, and it is what separates
                # ``no-reply@accounts.google.com`` from a colleague without
                # asking the model anything.
                "is_no_reply": is_no_reply(value) if kind == "sender" else False,
                "has_unsubscribe": False,
                "in_gap_set": False,
            }
            buckets[key] = row
        return row

    for from_email, from_domain, list_id, is_unread, unsubscribe_url in items:
        sender = (from_email or "").strip().lower()
        domain = (from_domain or _domain_of(sender)).strip().lower()
        targets = [
            bucket("sender", sender),
            bucket("domain", domain),
            bucket("list_id", (list_id or "")),
        ]
        for row in targets:
            if row is None:
                continue
            row["thread_count"] += 1
            if is_unread:
                row["unread_count"] += 1
            if unsubscribe_url:
                row["has_unsubscribe"] = True
            if sender and sender in replied:
                row["ever_replied"] = True
            if sender and sender in gap_senders:
                row["in_gap_set"] = True

    return sorted(
        buckets.values(), key=lambda r: (-r["thread_count"], r["kind"], r["value"])
    )


def _replied_addresses(session: Session, *, user_id: str) -> set[str]:
    """Addresses the user has genuinely replied to, from ``sender_profiles``.

    Post slice 2 this is trustworthy: the user's own address and its aliases are
    no longer harvested out of his own ``SENT`` mail, so ``ever_replied`` stops
    claiming "24 replies of 0 received" about himself. A ``no-reply`` sender is
    filtered here too — defence in depth, because a stale profile row may still
    assert a reply to an address that cannot receive one.
    """
    SenderProfile = _models().SenderProfile
    out: set[str] = set()
    for row in session.execute(
        select(SenderProfile.sender_email).where(
            SenderProfile.user_id == user_id, SenderProfile.ever_replied.is_(True)
        )
    ).scalars():
        address = (row or "").strip().lower()
        if address and not is_no_reply(address):
            out.add(address)
    return out


def _gap_senders(session: Session, *, user_id: str, run_id: str | None = None) -> set[str]:
    models = _models()
    Decision, Item = models.Decision, models.Item
    query = (
        select(Item.from_email)
        .join(Decision, Decision.item_id == Item.id)
        .where(Decision.user_id == user_id, _is_gap(Decision))
    )
    if run_id:
        query = query.where(Decision.run_id == run_id)
    return {(value or "").strip().lower() for value in session.execute(query).scalars() if value}


def build_gap_set(
    session: Session, *, user_id: str, run_id: str | None = None, limit: int = 500
) -> list[dict]:
    """The threads the current taxonomy failed on — the holes the proposal must close.

    Subjects are truncated to :data:`GAP_SUBJECT_MAX_CHARS`, exactly as the live
    feed already truncates them. Reasoning is included because *why* the
    classifier hesitated is the most useful thing the model can be told.
    """
    models = _models()
    Decision, Item = models.Decision, models.Item
    query = (
        select(
            Item.id,
            Item.from_email,
            Item.subject,
            Decision.status,
            Decision.autonomy_state,
            Decision.confidence,
            Decision.reasoning,
        )
        .join(Decision, Decision.item_id == Item.id)
        .where(Decision.user_id == user_id, _is_gap(Decision))
    )
    if run_id:
        query = query.where(Decision.run_id == run_id)
    out: list[dict] = []
    for item_id, from_email, subject, status, autonomy_state, confidence, reasoning in (
        session.execute(query.limit(limit)).tuples().all()
    ):
        out.append(
            {
                "item_id": item_id,
                "from_email": (from_email or "").strip().lower(),
                "subject": (subject or "")[:GAP_SUBJECT_MAX_CHARS],
                # Name the bucket the row is ACTUALLY in, whichever column holds it.
                "status": (
                    status
                    if status in GAP_STATUSES
                    else (autonomy_state if autonomy_state in GAP_STATUSES else status)
                ),
                "confidence": float(confidence or 0.0),
                "reasoning": (reasoning or "")[:200],
            }
        )
    return out


# ------------------------------------------------------- 2. the proposal


def _display_name(value: str) -> str:
    """A human name for a domain family: ``facebookmail.com`` -> ``Facebook``."""
    stem = value.split("@")[-1]
    parts = [p for p in stem.split(".") if p not in ("com", "org", "net", "co", "io", "in")]
    if not parts:
        parts = [stem]
    # Prefer the registrable-ish label, dropping mail/notification subdomains.
    noise = {"mail", "email", "mailer", "notifications", "notification", "updates", "e", "em"}
    meaningful = [p for p in parts if p not in noise] or parts
    core = meaningful[-1]
    for junk in ("mail", "email"):
        if core.endswith(junk) and len(core) > len(junk) + 2:
            core = core[: -len(junk)]
    return core.replace("-", " ").replace("_", " ").title()


def _key_for(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return key or "uncategorised"


def _deterministic_proposal(census: list[dict], current: list[dict]) -> list[dict]:
    """The evidence-only proposal: one category per concentrated domain family.

    This is what the user gets when the model is unavailable, and it is still a
    genuinely DERIVED answer — Apple, Facebook, PayPal and BookMyShow appear by
    name with their real thread counts. It is emphatically **not** the seed six
    dressed up as discovery; the honesty rule forbids that, because a generic
    list presented as "derived from your mail" is a lie the user cannot detect.

    The model's value-add over this is naming and *grouping* (BookMyShow +
    Jagriti Theatre -> "Events & Tickets"), not deciding what exists.
    """
    by_key: dict[str, dict] = {}

    # Carry forward the never-miss categories the user already has. These are not
    # inventions — they exist, they are load-bearing safety, and retiring them by
    # omission would be the single most destructive thing discovery could do.
    for category in current:
        if category["key"] in NEVER_ARCHIVE_KEYS:
            by_key[category["key"]] = {
                "key": category["key"],
                "name": category["name"],
                "description": category.get("description") or "",
                "default_action": "keep",
                "rationale": "Never-miss category, retained: mail a human has to see.",
                "evidence_senders": [],
                "covered_threads": 0,
                "retained_safety": True,
            }

    domains = [
        row
        for row in census
        if row["kind"] == "domain" and row["thread_count"] >= CONCENTRATION_MIN_THREADS
    ]
    senders_by_domain: dict[str, list[dict]] = {}
    for row in census:
        if row["kind"] == "sender":
            senders_by_domain.setdefault(row["from_domain"] or "", []).append(row)

    for domain_row in domains:
        domain = domain_row["value"]
        senders = [
            s
            for s in senders_by_domain.get(domain, [])
            if s["thread_count"] >= CONCENTRATION_MIN_THREADS
        ]
        # Genuine correspondence stays with people; a domain the user replies to
        # is not a bulk category.
        if domain_row["ever_replied"] and not domain_row["is_no_reply"]:
            continue
        name = _display_name(domain)
        key = _key_for(name)
        if key in by_key:
            continue
        evidence = [s["value"] for s in senders] or [domain]
        by_key[key] = {
            "key": key,
            "name": name,
            "description": (
                f"Automated mail from {name} ({domain}) — "
                f"{domain_row['thread_count']} threads in this mailbox."
            ),
            "default_action": "archive",
            "rationale": (
                f"{domain_row['thread_count']} threads from {domain} across "
                f"{len(evidence)} address(es); derived from your mail, not a default."
            ),
            "evidence_senders": evidence,
            "covered_threads": domain_row["thread_count"],
        }
    return list(by_key.values())


def _census_page(census: list[dict], page: int) -> list[dict]:
    start = page * MAX_SENDERS_PER_CALL
    return census[start : start + MAX_SENDERS_PER_CALL]


def _prompt(page: list[dict], gap_set: list[dict], current: list[dict]) -> str:
    instructions = _PROMPT_PATH.read_text(encoding="utf-8")
    census_lines = "\n".join(
        json.dumps(
            {
                "value": row["value"],
                "kind": row["kind"],
                "thread_count": row["thread_count"],
                "unread_count": row["unread_count"],
                "ever_replied": row["ever_replied"],
                "is_no_reply": row["is_no_reply"],
                "has_unsubscribe": row["has_unsubscribe"],
                "in_gap_set": row["in_gap_set"],
            },
            separators=(",", ":"),
        )
        for row in page
    )
    gap_lines = "\n".join(
        f"- [{g['status']} {g['confidence']:.2f}] {g['from_email']} :: {g['subject']}"
        for g in gap_set[:120]
    ) or "(none)"
    current_lines = "\n".join(
        f"- {c['key']} ({c['name']}): {c.get('description', '')}" for c in current
    ) or "(none)"
    return (
        f"{instructions}\n\n"
        f"## Current taxonomy\n{current_lines}\n\n"
        f"## Census ({len(page)} rows)\n{census_lines}\n\n"
        f"## Gap set — threads the current taxonomy could not place\n{gap_lines}\n"
    )


_PROPOSAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": [
            "key",
            "name",
            "description",
            "default_action",
            "rationale",
            "evidence_senders",
        ],
        "properties": {
            "key": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "default_action": {"type": "string", "enum": list(VALID_ACTIONS)},
            "rationale": {"type": "string"},
            "evidence_senders": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def _current_taxonomy(session: Session, user_id: str) -> list[dict]:
    Category = _models().Category
    return [
        {
            "id": row.id,
            "key": row.key,
            "name": row.name,
            "description": row.description or "",
            "default_action": row.default_action,
        }
        for row in session.execute(
            select(Category).where(Category.user_id == user_id).order_by(Category.sort_order)
        ).scalars()
    ]


def validate_proposal(proposal: list[dict], census: list[dict]) -> list[dict]:
    """Reject what the user must never be shown, and normalise the rest.

    Three refusals, all of them load-bearing:

    * **An evidence-free category is dropped.** "It may not invent a category
      with no evidence behind it" — a category naming no real sender is the model
      pattern-matching on generic inbox advice, which is the exact failure
      discovery exists to end. The single exception is a *retained* never-miss
      category that already exists on the account: keeping ``People`` is not an
      invention, and dropping it would be the one genuinely dangerous outcome.
    * **Evidence that is not in the census is dropped.** A hallucinated sender
      would mint a tier-1 rule that can never fire, which reads as "the feature
      does nothing" rather than as a bug.
    * **``NEVER_ARCHIVE_KEYS`` can never carry ``default_action="archive"``.**
      Coerced to ``keep`` here rather than raising, because a proposal is not a
      mutation and losing the whole proposal over one bad field would be worse;
      the write path guards it independently and unconditionally.
    """
    known = {row["value"]: row for row in census}
    out: list[dict] = []
    seen_keys: set[str] = set()
    claimed: set[str] = set()

    for raw in proposal or []:
        key = _key_for(str(raw.get("key") or raw.get("name") or ""))
        if not key or key in seen_keys:
            continue
        name = str(raw.get("name") or key.replace("_", " ").title()).strip()
        action = str(raw.get("default_action") or "keep")
        if action not in VALID_ACTIONS:
            action = "keep"
        if key in NEVER_ARCHIVE_KEYS and action == "archive":
            action = "keep"

        evidence: list[str] = []
        covered = 0
        for value in raw.get("evidence_senders") or []:
            value = str(value or "").strip().lower()
            row = known.get(value)
            # One sender, one category. A value claimed twice cannot become a
            # deterministic rule (there is no "dominant category" any more), so
            # first claim wins and the duplicate is dropped here rather than
            # silently producing two rules that fight.
            if row is None or value in claimed:
                continue
            claimed.add(value)
            evidence.append(value)
            if row["kind"] == "sender":
                covered += row["thread_count"]

        if not evidence and not raw.get("retained_safety"):
            continue

        seen_keys.add(key)
        out.append(
            {
                "key": key,
                "name": name,
                "description": str(raw.get("description") or "").strip(),
                "default_action": action,
                "rationale": str(raw.get("rationale") or "").strip(),
                "evidence_senders": evidence,
                "covered_threads": covered or int(raw.get("covered_threads") or 0),
                **({"retained_safety": True} if raw.get("retained_safety") else {}),
            }
        )
    return out


def _backfill_concentration(proposal: list[dict], census: list[dict]) -> list[dict]:
    """Place, deterministically, every concentrated sender the model forgot.

    The success criterion is absolute: **every sender contributing >= 10 threads
    must be named in some category's evidence list.** A model that drops one
    (measured: it dropped ``no-reply@accounts.google.com``, 39 threads) leaves a
    sender that will keep being asked of the LLM forever, keep landing in an
    ill-fitting generic bucket, and keep producing the ``below_threshold`` rows
    this phase exists to eliminate — invisibly, because nothing in the product
    would ever say so.

    So the census has the last word, exactly as it has the first: an unplaced
    sender joins the category that already claims its domain, and if no category
    claims its domain it gets one derived from the domain itself. Nothing is
    invented — the fallback category is named after a real domain with a real
    thread count.
    """
    claimed = {v for c in proposal for v in c["evidence_senders"]}
    unplaced = [
        row
        for row in census
        if row["kind"] == "sender"
        and row["thread_count"] >= CONCENTRATION_MIN_THREADS
        and row["value"] not in claimed
    ]
    if not unplaced:
        return proposal

    by_key = {c["key"]: c for c in proposal}
    domain_owner: dict[str, str] = {}
    for category in proposal:
        if category.get("retained_safety"):
            continue
        for value in category["evidence_senders"]:
            domain = _domain_of(value)
            if domain:
                domain_owner.setdefault(domain, category["key"])

    for row in unplaced:
        domain = row["from_domain"] or _domain_of(row["value"])
        # A correspondent is never swept into a bulk category by a backfill.
        if row["ever_replied"] and not row["is_no_reply"]:
            owner_key = "people"
        else:
            owner_key = domain_owner.get(domain) or ""
        owner = by_key.get(owner_key)
        if owner is None or owner.get("retained_safety"):
            name = _display_name(domain)
            key = _key_for(name)
            owner = by_key.get(key)
            if owner is None:
                owner = {
                    "key": key,
                    "name": name,
                    "description": (
                        f"Automated mail from {name} ({domain}) — derived from your "
                        "sender volumes."
                    ),
                    "default_action": "keep" if key in NEVER_ARCHIVE_KEYS else "archive",
                    "rationale": (
                        f"{row['thread_count']} threads from {row['value']} were not "
                        "placed by the model, so they were placed from your own volumes."
                    ),
                    "evidence_senders": [],
                    "covered_threads": 0,
                    "backfilled": True,
                }
                by_key[key] = owner
                proposal.append(owner)
            domain_owner.setdefault(domain, owner["key"])
        owner["evidence_senders"].append(row["value"])
        owner["covered_threads"] = int(owner.get("covered_threads") or 0) + row["thread_count"]
    return proposal


def _coverage(proposal: list[dict], census: list[dict], gap_set: list[dict]) -> dict:
    covered_values = {v for c in proposal for v in c["evidence_senders"]}
    senders = [row for row in census if row["kind"] == "sender"]
    total_threads = sum(row["thread_count"] for row in senders)
    covered_threads = sum(row["thread_count"] for row in senders if row["value"] in covered_values)
    concentrated = [row for row in senders if row["thread_count"] >= CONCENTRATION_MIN_THREADS]
    gap_resolved = sum(1 for g in gap_set if g["from_email"] in covered_values)
    return {
        "total_threads": total_threads,
        "covered_threads": covered_threads,
        "uncovered_threads": total_threads - covered_threads,
        "gap_threads_total": len(gap_set),
        "gap_threads_resolved": gap_resolved,
        # Named, never rounded away: the senders big enough to deserve a category
        # that the proposal nonetheless failed to place.
        "uncovered_concentrated_senders": sorted(
            row["value"] for row in concentrated if row["value"] not in covered_values
        ),
    }


def propose_taxonomy(session: Session, *, user_id: str, census: list[dict], gap_set: list[dict]) -> dict:
    """Census + gap set -> a proposal with per-category evidence and coverage.

    Returns::

        {"proposal": [...], "coverage": {...}, "partial": bool,
         "partial_reason": str | None}

    Chunked at :data:`MAX_SENDERS_PER_CALL` census rows per call, through the
    existing client (and therefore the existing model-fallback chain and
    throttle). **Discovery mutates nothing** — it produces a proposal; nothing is
    created, renamed, deleted or archived until the user approves.

    On total LLM failure the deterministic proposal is returned with
    ``partial=True`` and a stated reason. It never returns the seed six as if
    they were derived.
    """
    from llm.client import get_llm_client

    current = _current_taxonomy(session, user_id)
    deterministic = _deterministic_proposal(census, current)

    if not census:
        return {
            "proposal": [],
            "coverage": _coverage([], census, gap_set),
            "partial": True,
            "partial_reason": (
                "There is no mail to derive a taxonomy from yet — run a triage first."
            ),
        }

    client = get_llm_client()
    pages = max(1, (len(census) + MAX_SENDERS_PER_CALL - 1) // MAX_SENDERS_PER_CALL)
    raw: list[dict] = []
    failures: list[str] = []

    for page_index in range(pages):
        page = _census_page(census, page_index)
        if not page:
            continue
        try:
            result = client.call_model_sync(
                _prompt(page, gap_set, current),
                system=_SYSTEM,
                json_schema=_PROPOSAL_SCHEMA,
                max_tokens=6000,
                # Nemotron-class models otherwise spend the entire completion
                # budget on a reasoning chain and emit no JSON at all — the call
                # "succeeds", returns nothing parseable, and discovery silently
                # falls back to `partial`. Measured: 6000/6000 completion tokens,
                # zero categories. The schema already constrains the shape, so
                # the reasoning chain buys nothing here.
                disable_thinking=True,
            )
            parsed = _loads_array(result.text)
        except Exception as exc:  # noqa: BLE001 — a dead model degrades, never crashes
            failures.append(f"{type(exc).__name__}: {exc}")
            continue
        raw.extend(parsed)

    partial_reason: str | None = None
    if not raw:
        partial_reason = (
            "The model was unavailable, so these categories were derived from your "
            "sender volumes alone"
            + (f" ({failures[0]})" if failures else "")
            + "."
        )
        raw = deterministic
    elif failures:
        partial_reason = (
            f"{len(failures)} of {pages} census pages could not be sent to the model, "
            "so some of your smaller senders may be unplaced."
        )

    # The never-miss categories are merged in regardless of what the model said.
    # Discovery may rename or re-describe them; it may not retire them by
    # omission, and it may not set them to archive.
    proposal = validate_proposal(raw, census)
    proposed_keys = {c["key"] for c in proposal}
    for safety in deterministic:
        if safety.get("retained_safety") and safety["key"] not in proposed_keys:
            proposal.append(safety)

    # The census has the last word: nothing above the concentration bar is left
    # unplaced, whatever the model did or did not say.
    proposal = _backfill_concentration(proposal, census)

    return {
        "proposal": proposal,
        "coverage": _coverage(proposal, census, gap_set),
        "partial": partial_reason is not None,
        "partial_reason": partial_reason,
    }


def _loads_array(text: str) -> list[dict]:
    """Parse the model's array, tolerating a code fence or a wrapper object."""
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        for candidate in ("proposal", "categories", "taxonomy", "data"):
            if isinstance(parsed.get(candidate), list):
                parsed = parsed[candidate]
                break
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


# ------------------------------------------------- 3. concentration -> tier 1


def mine_sender_rules(census: list[dict], proposal: list[dict]) -> list[dict]:
    """Turn measured sender concentration into ORDINARY tier-1 ``Rule`` rows.

    This is the step the phase's success measure rests on. The five Facebook
    addresses (~1,586 threads), the two BookMyShow addresses (~635) and
    ``contact@jagrititheatre.com`` (334) — roughly half the mailbox — stop being
    asked of the model at all once these rows exist. They are matched by the
    **unchanged** ``tools.rules.apply_rules`` matcher, land ``decided_by="rule"``
    with ``rule_id`` set, at ``0.95`` confidence, at zero token cost.

    The bar is :data:`CONCENTRATION_MIN_THREADS` threads **and** a single
    dominant category — a sender claimed by two categories has no dominant
    category and is skipped rather than guessed at, because a wrong deterministic
    rule is worse than no rule: it is silent and it never asks.

    Returns row dicts (not ORM objects) so a caller can preview them before
    anything is written::

        {matcher, action, category_key, confidence, kind, source, status, name,
         evidence: {thread_count, value, kind}}
    """
    from domain.enums import RuleKind, RuleSource, RuleStatus

    by_value = {row["value"]: row for row in census}
    claim_count: dict[str, int] = {}
    for category in proposal:
        for value in category.get("evidence_senders") or []:
            claim_count[str(value).strip().lower()] = (
                claim_count.get(str(value).strip().lower(), 0) + 1
            )

    rules: list[dict] = []
    for category in proposal:
        key = category["key"]
        action_name = category.get("default_action", "keep")
        # Defence in depth. The write path guards this too, but a mined rule that
        # archives ``people`` must never even be *constructed*: it would be a
        # standing, silent sweep of the mail this product exists not to lose.
        if key in NEVER_ARCHIVE_KEYS and action_name == "archive":
            raise TaxonomyError(
                f"A mined rule can never archive into {key!r} — it is a never-miss "
                "category. A never-miss thread is archived individually, under its "
                "own label, with an undo token; the category is never swept."
            )
        for value in category.get("evidence_senders") or []:
            value = str(value).strip().lower()
            row = by_value.get(value)
            if row is None:
                continue
            if row["thread_count"] < CONCENTRATION_MIN_THREADS:
                continue
            if claim_count.get(value, 0) != 1:
                continue
            clause = {"sender": "from_email", "domain": "from_domain", "list_id": "list_id"}[
                row["kind"]
            ]
            matcher = normalise_matcher({clause: value})
            if not matcher:
                continue
            rules.append(
                {
                    "matcher": matcher,
                    "action": mined_rule_action(key, action_name),
                    "category_key": key,
                    "confidence": MINED_RULE_CONFIDENCE,
                    "kind": str(RuleKind.DETERMINISTIC),
                    "source": str(RuleSource.MINED),
                    "status": str(RuleStatus.ACTIVE),
                    "name": mined_rule_name(matcher, key),
                    # Carried so the user can see WHY the rule exists and disable
                    # it. A deterministic rule the user cannot interrogate is
                    # indistinguishable from the agent being wrong on purpose.
                    "evidence": {
                        "value": value,
                        "kind": row["kind"],
                        "thread_count": row["thread_count"],
                    },
                }
            )

    # A domain rule and a sender rule for the same mail would both fire; the
    # sender rule is strictly more specific, so drop the redundant domain rule.
    sender_domains = {
        _domain_of(r["matcher"]["from_email"]) for r in rules if "from_email" in r["matcher"]
    }
    return [
        r
        for r in rules
        if "from_domain" not in r["matcher"] or r["matcher"]["from_domain"] not in sender_domains
    ]


def materialise_rules(session: Session, *, user_id: str, rules: list[dict]) -> dict:
    """Write mined rules, idempotently. Returns ``{created, updated, skipped_user}``.

    Re-running discovery **updates the matching mined rule in place** rather than
    accumulating duplicates, and **never overwrites a ``source=user`` rule** — the
    user's own rule outranks anything mined, always, and silently rewriting it
    would be the agent overruling an explicit instruction.

    Every rule is validated against the same
    :func:`tools.taxonomy.validate_default_action` guard the taxonomy editor uses,
    **before** anything is written: a rule archiving into a never-miss category is
    rejected at materialisation, not written and filtered later.
    """
    models = _models()
    Rule, Category = models.Rule, models.Category

    categories = {
        row.key: row
        for row in session.execute(
            select(Category).where(Category.user_id == user_id)
        ).scalars()
    }

    # Validate the WHOLE batch first, so a bad row cannot leave half of a
    # re-discovery written.
    prepared: list[tuple[dict, str]] = []
    for rule in rules or []:
        matcher = normalise_matcher(rule.get("matcher"))
        if not matcher:
            raise DiscoveryError(
                f"mined rule {rule.get('name')!r} has no usable matcher clause "
                f"(expected one of {list(MINED_MATCHER_CLAUSES)})"
            )
        key = rule.get("category_key")
        if key not in categories:
            raise DiscoveryError(
                f"mined rule {rule.get('name')!r} targets unknown category {key!r} — "
                "approve the taxonomy before materialising its rules."
            )
        action = rule.get("action") or {}
        proposed = "archive" if action.get("archive") else ("digest" if action.get("digest") else "keep")
        validate_default_action(key, proposed)
        prepared.append((rule, key))

    existing = list(
        session.execute(select(Rule).where(Rule.user_id == user_id)).scalars()
    )
    by_matcher: dict[str, list] = {}
    for row in existing:
        by_matcher.setdefault(_matcher_signature(row.matcher), []).append(row)

    created = updated = skipped_user = 0
    for rule, key in prepared:
        matcher = normalise_matcher(rule["matcher"])
        signature = _matcher_signature(matcher)
        candidates = by_matcher.get(signature, [])
        user_owned = [r for r in candidates if (r.source or "") not in ("mined",)]
        mine = [r for r in candidates if (r.source or "") == "mined"]

        if user_owned:
            # The user's rule wins and is left byte-identical. Minting a second,
            # mined rule for the same matcher would be worse than useless: the
            # first match wins in `apply_rules`, so it would be dead weight the
            # user has to reason about.
            skipped_user += 1
            continue

        if mine:
            row = mine[0]
            row.name = rule["name"]
            row.action = rule["action"]
            row.confidence = float(rule["confidence"])
            row.status = rule["status"]
            row.kind = rule["kind"]
            updated += 1
            continue

        row = Rule(
            user_id=user_id,
            name=rule["name"],
            kind=rule["kind"],
            source=rule["source"],
            matcher=matcher,
            action=rule["action"],
            status=rule["status"],
            confidence=float(rule["confidence"]),
        )
        session.add(row)
        by_matcher.setdefault(signature, []).append(row)
        created += 1

    session.flush()
    return {"created": created, "updated": updated, "skipped_user": skipped_user}


def _matcher_signature(matcher: dict | None) -> str:
    """Comparable identity of a matcher, independent of key order and casing."""
    return json.dumps(normalise_matcher(matcher), sort_keys=True)
