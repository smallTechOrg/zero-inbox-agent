"""A 10,000-decision mailbox — the re-organisation fixture (Phase 9, slice 5).

Ten thousand rows is not decoration. The re-organiser's one real failure mode is
a **silent sample**: capping at the first N, or quietly dropping the tail, and
reporting a tidy ledger over work it never did. At ten thousand rows a sampled
run and a full run give observably different numbers, so the arithmetic
``done + sum(skipped.values()) == 10000`` cannot be satisfied by accident.

The sender shape replays the **measured** concentration on the user's live
account — Facebook, BookMyShow, Jagriti Theatre, Apple, PayPal, Twitter — because
that concentration is the entire reason Phase 9's mined tier-1 rules resolve most
of a mailbox with zero tokens. A fixture of uniform random senders would let a
re-organiser pass that only works when nothing is concentrated.

Every bucket below is deliberate, and each one is the ledger outcome it is named
after:

===================  ======  ==================================================
bucket                count  what the re-organiser must do with it
===================  ======  ==================================================
``already_correct``   5,000  target category == current category → skip, no call
``relabel_archived``  3,000  archived, category changed → relabel, stays out of
                             the inbox (``INBOX`` never re-added)
``relabel_inbox``     1,000  in the inbox, category changed → relabel in place
``not_reviewed``        500  ``provisional`` / ``review_failed`` → never mutated
``no_category_fit``     400  matches no rule → named, never guessed
``gmail_error``         100  Gmail refuses → named, never silently dropped
===================  ======  ==================================================

``done`` is therefore **4,000** and ``sum(skipped.values())`` is **6,000**.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from random import Random

#: Fixed so the fixture is deterministic: the same 10,000 rows, in the same
#: order, on every machine and every run. A fixture that reshuffles itself turns
#: a real regression into "probably a flake".
PLAN_SEED = 20250915

#: Reserved test identity — ``tests/isolation.py`` refuses anything else.
USER_ID = "test-reorg-10k-user"
USER_EMAIL = "reorg-10k@example.com"
ACCOUNT_EMAIL = "reorg-10k-mailbox@example.com"

TOTAL = 10_000

#: The measured live concentration, in the proportions it was measured in. The
#: five Facebook addresses, the two BookMyShow addresses and Jagriti Theatre are
#: the real ones; they are what makes tier 1 carry the job.
CONCENTRATED_SENDERS: tuple[tuple[str, str, str], ...] = (
    ("notification@facebookmail.com", "facebookmail.com", "social"),
    ("notification+kr4knbaqrsga@facebookmail.com", "facebookmail.com", "social"),
    ("reminders@facebookmail.com", "facebookmail.com", "social"),
    ("friendsuggestion@facebookmail.com", "facebookmail.com", "social"),
    ("notification@priority.facebookmail.com", "priority.facebookmail.com", "social"),
    ("no-reply@entertainment.bookmyshow.com", "entertainment.bookmyshow.com", "events"),
    ("no-reply@updates.bookmyshow.com", "updates.bookmyshow.com", "events"),
    ("contact@jagrititheatre.com", "jagrititheatre.com", "events"),
    ("no_reply@email.apple.com", "email.apple.com", "billing"),
    ("noreply@email.apple.com", "email.apple.com", "billing"),
    ("service@paypal.com", "paypal.com", "billing"),
    ("info@twitter.com", "twitter.com", "social"),
)

#: Senders no mined rule covers — the honest ``no_category_fit`` tail. A real
#: mailbox always has one, and inventing a category for it would be worse than
#: saying "these did not fit".
UNMATCHED_SENDERS: tuple[str, ...] = (
    "a.human@partner.example.com",
    "someone.else@another.example.com",
)

#: key -> (name, default_action)
CATEGORIES: dict[str, tuple[str, str]] = {
    "social": ("Social", "archive"),
    "events": ("Events", "archive"),
    "billing": ("Billing", "archive"),
    "notifications": ("Notifications", "archive"),
    "newsletters": ("Newsletters", "archive"),
    "people": ("People", "keep"),
    "urgent": ("Urgent", "keep"),
}

#: The counts each bucket contributes. Ordered so the table above and the code
#: can never drift.
BUCKET_SIZES: dict[str, int] = {
    "already_correct": 5_000,
    "relabel_archived": 3_000,
    "relabel_inbox": 1_000,
    "not_reviewed": 500,
    "no_category_fit": 400,
    "gmail_error": 100,
}

EXPECTED_DONE = BUCKET_SIZES["relabel_archived"] + BUCKET_SIZES["relabel_inbox"]
EXPECTED_SKIPPED: dict[str, int] = {
    "already_correct": BUCKET_SIZES["already_correct"],
    "not_reviewed": BUCKET_SIZES["not_reviewed"],
    "no_category_fit": BUCKET_SIZES["no_category_fit"],
    "gmail_error": BUCKET_SIZES["gmail_error"],
}

INBOX = "INBOX"


def _label_id(key: str) -> str:
    return f"Label_{key}"


def seed(session, *, user_id: str = USER_ID, total: int = TOTAL) -> dict:
    """Build the whole fixture and return the map the tests assert against.

    Returns ``{"user_id", "account_id", "run_id", "categories", "buckets",
    "expected_done", "expected_skipped", "error_thread_ids"}`` where ``buckets``
    maps a bucket name to the list of decision ids in it — so a test can name the
    exact row that came out wrong rather than only the count.
    """
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        Rule,
        TriageRun,
        User,
        UserSettings,
    )

    if total != TOTAL:  # a smaller fixture must still keep the proportions honest
        raise ValueError(
            "the re-organisation fixture is 10,000 rows by design — a smaller one "
            "cannot tell a full run from a sampled one, which is the whole point"
        )

    session.add(User(id=user_id, email=USER_EMAIL, display_name="Reorg 10k"))
    session.add(UserSettings(user_id=user_id, dry_run=False))
    account = ChannelAccount(
        id="test-reorg-10k-account",
        user_id=user_id,
        channel="gmail",
        account_email=ACCOUNT_EMAIL,
        refresh_token_enc="test-not-a-real-token",
    )
    session.add(account)

    categories: dict[str, Category] = {}
    for order, (key, (name, default_action)) in enumerate(CATEGORIES.items()):
        category = Category(
            id=f"test-cat-{key}",
            user_id=user_id,
            key=key,
            name=name,
            channel_label_name=f"ZeroInbox/{name}",
            channel_label_id=_label_id(key),
            default_action=default_action,
            sort_order=order,
        )
        categories[key] = category
        session.add(category)

    # Mined-shaped tier-1 rules: one per concentrated sender, exactly what
    # slice 4's `mine_sender_rules` materialises. They are read by the UNCHANGED
    # `tools.rules.apply_rules` matcher — the re-organiser has no matcher of its
    # own, and this fixture is what proves that seam carries the job.
    for index, (email, _domain, category_key) in enumerate(CONCENTRATED_SENDERS):
        session.add(
            Rule(
                id=f"test-rule-{index}",
                user_id=user_id,
                name=f"mined:{email}",
                kind="deterministic",
                source="mined",
                matcher={"from_email": email},
                action={
                    "set_category": category_key,
                    "archive": CATEGORIES[category_key][1] == "archive",
                },
                status="active",
                confidence=0.98,
            )
        )

    run = TriageRun(
        id="test-reorg-10k-run",
        user_id=user_id,
        channel_account_id=account.id,
        status="completed",
        dry_run=False,
        items_total=total,
        items_decided=total,
    )
    session.add(run)
    session.flush()

    buckets: dict[str, list[str]] = {name: [] for name in BUCKET_SIZES}
    error_thread_ids: list[str] = []
    base_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

    plan: list[str] = []
    for bucket, size in BUCKET_SIZES.items():
        plan.extend([bucket] * size)
    assert len(plan) == total, "the bucket sizes must add up to the fixture total"
    # Interleaved, with a fixed seed so the fixture is byte-identical every run.
    # A block-ordered plan would make every partial run unrepresentative — kill a
    # block-ordered job at 40 % and it has done nothing but `already_correct`
    # rows, so a resume test would prove nothing about resuming real work.
    Random(PLAN_SEED).shuffle(plan)

    items: list[Item] = []
    decisions: list[Decision] = []

    for index, bucket in enumerate(plan):
        # Zero-padded so lexical order over `item_id` — which is what the
        # re-organiser's resume cursor uses — is a stable, total order.
        item_id = f"test-item-{index:06d}"
        thread_id = f"thread-{index:06d}"

        if bucket == "no_category_fit":
            sender = UNMATCHED_SENDERS[index % len(UNMATCHED_SENDERS)]
            domain = sender.split("@", 1)[1]
            target_key = None
        else:
            sender, domain, target_key = CONCENTRATED_SENDERS[
                index % len(CONCENTRATED_SENDERS)
            ]

        if bucket == "already_correct":
            current_key = target_key
        elif target_key is None:
            current_key = "notifications"
        else:
            # Every one of these was filed under the pre-discovery generic
            # category — which is exactly why the user asked for a
            # re-organisation in the first place.
            current_key = "notifications" if target_key != "notifications" else "newsletters"

        in_inbox = bucket in ("relabel_inbox", "not_reviewed", "no_category_fit")
        labels = [_label_id(current_key)]
        if in_inbox:
            labels.insert(0, INBOX)

        items.append(
            Item(
                id=item_id,
                user_id=user_id,
                channel_account_id=account.id,
                external_thread_id=thread_id,
                subject=f"Fixture thread {index}",
                from_name="Fixture Sender",
                from_email=sender,
                from_domain=domain,
                snippet_redacted="",
                internal_date=base_date + timedelta(minutes=index),
                channel_labels=sorted(labels),
            )
        )

        review_state = "reviewed"
        if bucket == "not_reviewed":
            # Half provisional, half review_failed: both must be refused, and a
            # test that only covers one of them would miss the recovery path.
            review_state = "provisional" if index % 2 == 0 else "review_failed"

        decision = Decision(
            id=f"test-decision-{index:06d}",
            user_id=user_id,
            item_id=item_id,
            run_id=run.id,
            category_id=categories[current_key].id,
            proposed_action="archive" if not in_inbox else "keep",
            confidence=0.9,
            reasoning="",
            decided_by="rule",
            status="applied" if not in_inbox else "proposed",
            review_state=review_state,
            autonomy_state="auto_act" if not in_inbox else "category_keep",
        )
        decisions.append(decision)
        buckets[bucket].append(decision.id)
        if bucket == "gmail_error":
            error_thread_ids.append(thread_id)

    session.add_all(items)
    session.add_all(decisions)
    session.flush()

    return {
        "user_id": user_id,
        "account_id": account.id,
        "run_id": run.id,
        "categories": {key: category.id for key, category in categories.items()},
        "label_ids": {key: _label_id(key) for key in CATEGORIES},
        "buckets": buckets,
        "expected_done": EXPECTED_DONE,
        "expected_skipped": dict(EXPECTED_SKIPPED),
        "error_thread_ids": error_thread_ids,
        "total": total,
    }
