"""The 365-row remainder fixture — the user's real inbox floor, replayed exactly.

**Measured on the live account after the last Phase 8 run** (`spec/roadmap.md`
§ Phase 9). These five numbers are the reason this phase exists:

| bucket                | count |
|-----------------------|------:|
| `held_by_never_miss`  |   227 |
| `category_keep`       |    76 |
| `below_threshold`     |    46 |
| `needs_your_call`     |    16 |
| `unclassified`        |     0 |
| **total**             | **365** |

Target: **0**.

**The full set, never a sample.** 365 rows are built, every time. A sampled
answer and a full answer are observably different — 227 is not reachable from a
sample, and neither is the 186/23/18 breakdown of *why* those 227 were held.

## What this fixture encodes, and what it deliberately does not

It encodes **causes**, not conclusions. Each thread carries the decision the
pre-Phase-9 pipeline actually made about it; the buckets above are then produced
by running the **real** `graph.autonomy.classify_autonomy_state` over them
(:func:`classify_measured_distribution`). Asserting the distribution into
existence would prove nothing — the point is that the real policy, unchanged,
still reproduces the measured floor.

The 227 break down by `decided_by` exactly as measured — **llm 186 · reviewer 23
· sender_history 18** — because the three have three different fates in Phase 9:

* **186 `llm` / `time_sensitive`** — automated no-reply senders correctly judged
  time-sensitive, at the five measured live addresses. The verdict was right;
  only its expression was wrong. These become `ZeroInbox/Urgent`.
* **23 `reviewer`** — the second-pass reviewer said the user would be upset to
  miss them, for reasons that are neither "a person" nor "a deadline". These
  become `ZeroInbox/Important`, the label Phase 9 adds precisely because there
  was nowhere to put them.
* **18 `sender_history`** — **the bug.** All 18 are from the account's own
  address, held with the signature *"24 replies of 0 received"*: a real
  correspondent also *sends* you mail. Post-fix they are not held at all, so the
  same fixture yields **227 holds under** :func:`legacy_sender_stats` **and 209
  under** :func:`fixed_sender_stats`. The bug dying is a number that moves, not
  a claim.

The live account's own address is deny-listed by ``tests/isolation.py`` and is
never used here; ``founder@example.com`` plays the self-address role, which is
the same defect (the account harvesting itself out of its own ``SENT`` mail) in
a non-routable mailbox.

## Sender concentration

The senders are the measured ones — five Facebook addresses, two BookMyShow
addresses, Jagriti Theatre, Apple, Google. :data:`CONCENTRATION_RULES` are
tier-1 `Rule` rows of exactly the shape `tools.taxonomy_discovery.mine_sender_rules`
mints, matched by the **unchanged** `tools.rules.apply_rules`. They are what
drives `category_keep`, `below_threshold` and `needs_your_call` to zero: the
concentrated automated mail stops being *asked* of the model, so it can no
longer land under the floor or under the bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TOTAL = 365

#: The measured live distribution. Do not re-derive; do not query the live DB.
MEASURED_DISTRIBUTION: dict[str, int] = {
    "held_by_never_miss": 227,
    "category_keep": 76,
    "below_threshold": 46,
    "needs_your_call": 16,
    "unclassified": 0,
}

#: The measured `decided_by` breakdown of the 227.
HELD_BY_DECIDED_BY: dict[str, int] = {"llm": 186, "reviewer": 23, "sender_history": 18}

USER_ID = "test-remainder-365"
ACCOUNT_ID = "test-acct-remainder-365"

#: The connected account's own address. Stands in for the live account's mailbox,
#: which ``tests/isolation.py`` deny-lists. The defect is identical: `SENT` mail
#: addressed to yourself makes you your own most-replied-to correspondent.
ACCOUNT_EMAIL = "founder@example.com"

#: The five measured live no-reply senders that produced most of the 227.
NO_REPLY_SENDERS: tuple[str, ...] = (
    "no_reply@email.apple.com",
    "noreply@email.apple.com",
    "no-reply@accounts.google.com",
    "reminders@facebookmail.com",
    "security@facebookmail.com",
)

#: The measured Facebook / BookMyShow / Jagriti concentration.
FACEBOOK_SENDERS: tuple[str, ...] = (
    "notification@facebookmail.com",
    "notification+kr4knbaqrsga@facebookmail.com",
    "friendsuggestion@facebookmail.com",
)
BOOKMYSHOW_SENDERS: tuple[str, ...] = (
    "no-reply@entertainment.bookmyshow.com",
    "no-reply@updates.bookmyshow.com",
)
JAGRITI_SENDER = "contact@jagrititheatre.com"

#: Same defaults the product ships with. The floor is what makes a thread
#: `needs_your_call`; the bar is what makes it `below_threshold`.
CONFIDENCE_FLOOR = 0.75
AUTO_ACT_THRESHOLD = 0.80

SETTINGS: dict = {"confidence_floor": CONFIDENCE_FLOOR, "auto_act_threshold": AUTO_ACT_THRESHOLD}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# The 365 threads and the decisions the pre-Phase-9 pipeline made about them
# --------------------------------------------------------------------------


def _thread(index: int, *, subject: str, from_email: str, snippet: str) -> dict:
    return {
        "id": f"rem-item-{index:04d}",
        "external_thread_id": f"rem-thread-{index:04d}",
        "external_message_ids": [f"rem-msg-{index:04d}"],
        "subject": subject,
        "from_name": from_email.split("@")[0],
        "from_email": from_email,
        "from_domain": from_email.split("@")[-1],
        "to_emails": [ACCOUNT_EMAIL],
        "cc_emails": [],
        "list_id": None,
        "unsubscribe_url": None,
        "message_count": 1,
        "has_attachments": False,
        "snippet": snippet,
        "snippet_redacted": snippet[:200],
        "internal_date": _now() - timedelta(days=index % 30),
        "is_unread": index % 3 == 0,
        "channel_labels": ["INBOX"],
    }


def _decision(item_id: str, **fields) -> dict:
    base = {
        "item_id": item_id,
        "category": "notifications",
        "proposed_action": "archive",
        "confidence": 0.88,
        "reasoning": "measured pre-Phase-9 verdict",
        "decided_by": "llm",
        "time_sensitive": False,
        "status": "proposed",
    }
    base.update(fields)
    return base


def build_remainder() -> tuple[list[dict], list[dict]]:
    """``(items, decisions)`` — 365 of each, in the measured composition.

    The decisions are the ones the pre-Phase-9 pipeline actually produced. The
    buckets are *derived* from them by the real policy, never asserted here.
    """
    items: list[dict] = []
    decisions: list[dict] = []
    index = 0

    def add(*, subject: str, from_email: str, snippet: str, **decision_fields) -> None:
        nonlocal index
        item = _thread(index, subject=subject, from_email=from_email, snippet=snippet)
        items.append(item)
        decisions.append(_decision(item["id"], **decision_fields))
        index += 1

    # --- 186 held_by_never_miss, decided_by="llm": time-sensitive automated mail
    # The verdict is correct. Under the old semantic its only expression was
    # "leave it in the inbox", which is how 200+ notices floored the inbox.
    urgent_subjects = (
        "Security alert: new sign-in from an unrecognised device",
        "Your Apple ID was used to sign in on a new device",
        "Action required: confirm this sign-in attempt",
        "Reminder: your event starts in 2 hours",
        "Unusual activity detected on your account",
    )
    for n in range(186):
        add(
            subject=urgent_subjects[n % len(urgent_subjects)],
            from_email=NO_REPLY_SENDERS[n % len(NO_REPLY_SENDERS)],
            snippet="Immediate action may be required to keep your account secure.",
            time_sensitive=True,
            confidence=0.88,
            decided_by="llm",
        )

    # --- 23 held_by_never_miss, decided_by="reviewer": the second-pass flips.
    # Neither a person nor a deadline — Phase 9 adds `Important` for exactly this.
    for n in range(23):
        add(
            subject=f"Changes to your subscription terms ({n})",
            from_email=BOOKMYSHOW_SENDERS[n % len(BOOKMYSHOW_SENDERS)],
            snippet="We are updating the terms of your account from next month.",
            proposed_action="keep",
            decided_by="reviewer",
            confidence=0.86,
            reasoning="Reviewer: the user would want to see this before it is filed.",
        )

    # --- 18 held_by_never_miss, decided_by="sender_history": THE BUG.
    # Mail from the account's own address, held because `_accumulate_recipients`
    # harvested `To`/`Cc` out of the user's own SENT mail. Post-fix these are not
    # held at all, which is why the same fixture yields 227 then 209.
    for n in range(18):
        add(
            subject=f"Note to self: {['renew domain', 'call back', 'read later'][n % 3]}",
            from_email=ACCOUNT_EMAIL,
            snippet="Reminder I mailed myself. 24 replies of 0 received is the signature.",
            confidence=0.88,
            decided_by="llm",
        )

    # --- 76 category_keep: concentrated automated mail landing in a keep category.
    # Jagriti Theatre alone was 334 threads on the live account, all collapsed
    # into a generic category that says "keep".
    for n in range(76):
        add(
            subject=f"This weekend at Jagriti: show {n}",
            from_email=JAGRITI_SENDER,
            snippet="Book your seats for this weekend's performance.",
            category="people",
            proposed_action="keep",
            confidence=0.90,
        )

    # --- 46 below_threshold: above the floor, under the autonomy bar.
    for n in range(46):
        add(
            subject=f"You have {n + 1} new notifications",
            from_email=FACEBOOK_SENDERS[n % len(FACEBOOK_SENDERS)],
            snippet="See what you missed on Facebook.",
            confidence=0.78,
        )

    # --- 16 needs_your_call: no category fit, so confidence fell under the floor.
    for n in range(16):
        add(
            subject=f"(no subject) {n}",
            from_email=FACEBOOK_SENDERS[n % len(FACEBOOK_SENDERS)],
            snippet="see attached",
            confidence=0.62,
        )

    assert len(items) == TOTAL, f"the remainder fixture is the FULL 365, got {len(items)}"
    assert len(decisions) == TOTAL
    return items, decisions


# --------------------------------------------------------------------------
# Sender history — before and after the correspondent-truth fix
# --------------------------------------------------------------------------


def legacy_sender_stats() -> dict[str, dict]:
    """Sender history as the **pre-Phase-9** adapter harvested it.

    The account's own address carries an `ever_replied` claim with the measured
    *"24 replies of 0 received"* signature — a claim no genuine correspondent
    could produce, because a correspondent also sends you mail.
    """
    return {
        ACCOUNT_EMAIL: {
            "ever_replied": True,
            "replied_count": 24,
            "received_count": 0,
        }
    }


def fixed_sender_stats() -> dict[str, dict]:
    """Sender history as the **post-slice-2** adapter harvests it.

    The account's own address and its aliases are excluded from accumulation
    entirely, so there is no reply signal to inherit. No-reply senders never had
    one and still do not.
    """
    return {}


# --------------------------------------------------------------------------
# The measured distribution, produced by the REAL policy
# --------------------------------------------------------------------------


def classify_measured_distribution(
    items: list[dict],
    decisions: list[dict],
    *,
    sender_stats: dict[str, dict] | None = None,
    categories: dict[str, dict] | None = None,
) -> dict[str, int]:
    """Run the real never-miss floor + the real autonomy policy over the fixture.

    Nothing is asserted into existence: the buckets fall out of
    ``tools.never_miss.apply_confidence_floor`` and
    ``graph.autonomy.classify_autonomy_state``, both unchanged by this phase.
    """
    from graph.autonomy import classify_autonomy_state
    from tools.never_miss import apply_confidence_floor

    categories = categories or seed_category_index()
    items_by_id = {i["id"]: i for i in items}
    floored = apply_confidence_floor(decisions, CONFIDENCE_FLOOR)

    tally: dict[str, int] = dict.fromkeys(MEASURED_DISTRIBUTION, 0)
    for decision in floored:
        state = classify_autonomy_state(
            decision,
            categories.get(decision.get("category")),
            SETTINGS,
            sender_stats or {},
            {},
            item=items_by_id.get(decision["item_id"]),
        )
        tally[state] = tally.get(state, 0) + 1
    return tally


def seed_category_index() -> dict[str, dict]:
    """The seed taxonomy as ``state["categories"]`` carries it."""
    from tools.rules import DEFAULT_TAXONOMY

    return {
        category["key"]: {
            "key": category["key"],
            "name": category["name"],
            "channel_label_name": f"ZeroInbox/{category['name']}",
            "default_action": category["default_action"],
            "auto_act_threshold": None,
        }
        for category in DEFAULT_TAXONOMY
    }


# --------------------------------------------------------------------------
# Tier-1 concentration rules — the existing machinery, no second classifier
# --------------------------------------------------------------------------

#: `Rule` rows of exactly the shape a mined sender rule takes, matched by the
#: **unchanged** `tools.rules.apply_rules`. Every sender here cleared the
#: measured >= 10-thread concentration bar. This is what removes the question
#: from the model, which is what drives `below_threshold`, `needs_your_call` and
#: `category_keep` to zero — deterministically rather than hopefully.
#: `mine_sender_rules` deliberately does NOT cover the five no-reply senders that
#: produced the 186 time-sensitive holds, nor the 23 the reviewer flipped: those
#: are the genuine long tail, and they are exactly the mail the LLM should still
#: be asked about. Their route to zero is the reframe, not a rule.
CONCENTRATION_RULES: tuple[dict, ...] = tuple(
    {
        "name": f"Mined: {sender}",
        "matcher": {"from_email": sender},
        "action": {"set_category": "notifications", "archive": True},
        "confidence": 0.96,
    }
    for sender in (*FACEBOOK_SENDERS, JAGRITI_SENDER)
)


def pre_review(decisions: list[dict]) -> tuple[list[dict], set[str]]:
    """The decisions as they ENTER the second-pass reviewer, and what it flips.

    :func:`build_remainder` returns the measured *post*-reviewer state: the 23
    reviewer holds already read ``decided_by="reviewer"`` / ``keep``. Replaying a
    run needs the state before that, because a reviewer hold is only reviewable —
    and therefore only ever ``review_state="reviewed"``, and therefore only ever
    appliable — if it was an ``archive`` proposal when the reviewer saw it.

    That is not a fixture detail, it is the safety property: the never-miss
    archive of a reviewer-held thread is audited *because* the reviewer is what
    held it.
    """
    out: list[dict] = []
    flips: set[str] = set()
    for decision in decisions:
        decision = dict(decision)
        if decision.get("decided_by") == "reviewer":
            flips.add(decision["item_id"])
            decision["decided_by"] = "llm"
            decision["proposed_action"] = "archive"
            decision["reasoning"] = "measured pre-Phase-9 verdict"
        out.append(decision)
    return out, flips


def phase9_decisions(items: list[dict], decisions: list[dict]) -> list[dict]:
    """The same 365 threads, re-decided with the concentration rules in place.

    Runs the **unchanged** ``tools.rules.apply_rules`` — the existing tier-1
    matcher, the existing node's input shape. Phase 9 adds no second
    classification path, and a fixture that fabricated one would make the whole
    "the concentration was exploited deterministically" claim untestable.

    Everything a rule resolves lands ``decided_by="rule"`` at 0.96 — well above
    both the 0.75 floor and the 0.80 bar — which is precisely why
    ``below_threshold`` and ``needs_your_call`` go to zero: the mail that
    produced them is no longer being asked of the model.
    """
    from tools.rules import apply_rules

    rule_rows = [
        {
            "id": f"rem-rule-{n}",
            "matcher": rule["matcher"],
            "action": rule["action"],
            "status": "active",
            "confidence": rule["confidence"],
        }
        for n, rule in enumerate(CONCENTRATION_RULES)
    ]
    resolved, _unresolved = apply_rules(items, rule_rows)
    by_item = {d["item_id"]: d for d in resolved}
    return [by_item.get(d["item_id"], d) for d in decisions]


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def seed_remainder_user(session, *, with_concentration_rules: bool = True) -> None:
    """Seed the user, mailbox, full Phase 9 taxonomy and sender history.

    ``SenderProfile.ever_replied`` is **false** for the account's own address —
    that is the post-slice-2 truth, and the assertion the gate makes. A stale
    row asserting otherwise is exercised separately, against the guard layer.
    """
    from db.models import Category, ChannelAccount, Rule, SenderProfile, User, UserSettings
    from tools.rules import DEFAULT_TAXONOMY

    session.add(User(id=USER_ID, email=ACCOUNT_EMAIL, display_name="Founder"))
    session.add(
        ChannelAccount(
            id=ACCOUNT_ID,
            user_id=USER_ID,
            channel="gmail",
            account_email=ACCOUNT_EMAIL,
            refresh_token_enc="encrypted",
            scopes=["gmail.readonly"],
            status="connected",
        )
    )
    session.add(
        UserSettings(
            user_id=USER_ID,
            confidence_floor=CONFIDENCE_FLOOR,
            auto_act_threshold=AUTO_ACT_THRESHOLD,
            dry_run=False,
        )
    )
    for order, category in enumerate(DEFAULT_TAXONOMY):
        session.add(
            Category(
                id=f"rem-cat-{category['key']}",
                user_id=USER_ID,
                key=category["key"],
                name=category["name"],
                description=category["description"],
                channel_label_name=f"ZeroInbox/{category['name']}",
                default_action=category["default_action"],
                is_default=True,
                sort_order=order,
            )
        )

    # The account's own address: received mail, and NOT a correspondent.
    session.add(
        SenderProfile(
            user_id=USER_ID,
            sender_email=ACCOUNT_EMAIL,
            sender_domain=ACCOUNT_EMAIL.split("@")[-1],
            received_count=18,
            opened_count=18,
            replied_count=0,
            ever_replied=False,
            last_seen_at=_now(),
            importance_score=0.1,
        )
    )
    for sender in NO_REPLY_SENDERS:
        session.add(
            SenderProfile(
                user_id=USER_ID,
                sender_email=sender,
                sender_domain=sender.split("@")[-1],
                received_count=44,
                opened_count=4,
                replied_count=0,
                ever_replied=False,
                last_seen_at=_now(),
                importance_score=0.2,
            )
        )

    if with_concentration_rules:
        for n, rule in enumerate(CONCENTRATION_RULES):
            session.add(
                Rule(
                    id=f"rem-rule-{n}",
                    user_id=USER_ID,
                    name=rule["name"],
                    kind="deterministic",
                    source="mined",
                    matcher=rule["matcher"],
                    action=rule["action"],
                    status="active",
                    confidence=rule["confidence"],
                )
            )


def seed_remainder_items(session, items: list[dict]) -> None:
    """Persist the 365 items so decisions can reference real rows."""
    from db.models import Item

    for item in items:
        session.add(
            Item(
                id=item["id"],
                user_id=USER_ID,
                channel_account_id=ACCOUNT_ID,
                external_thread_id=item["external_thread_id"],
                external_message_ids=item["external_message_ids"],
                subject=item["subject"],
                from_name=item["from_name"],
                from_email=item["from_email"],
                from_domain=item["from_domain"],
                message_count=1,
                snippet_redacted=item["snippet_redacted"],
                internal_date=item["internal_date"],
                is_unread=item["is_unread"],
                channel_labels=["INBOX"],
            )
        )


__all__ = [
    "ACCOUNT_EMAIL",
    "ACCOUNT_ID",
    "AUTO_ACT_THRESHOLD",
    "BOOKMYSHOW_SENDERS",
    "CONCENTRATION_RULES",
    "CONFIDENCE_FLOOR",
    "FACEBOOK_SENDERS",
    "HELD_BY_DECIDED_BY",
    "JAGRITI_SENDER",
    "MEASURED_DISTRIBUTION",
    "NO_REPLY_SENDERS",
    "SETTINGS",
    "TOTAL",
    "USER_ID",
    "build_remainder",
    "classify_measured_distribution",
    "fixed_sender_stats",
    "legacy_sender_stats",
    "phase9_decisions",
    "pre_review",
    "seed_category_index",
    "seed_remainder_items",
    "seed_remainder_user",
]
