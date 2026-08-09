"""A deterministic fixture of 220 real-shaped inbox threads.

Shapes and proportions mirror a real founder-type Gmail inbox: a heavy newsletter and
app-notification tail, a mid-band of receipts / recruiter outreach / cold sales, and a
thin, high-value band of real human mail and time-sensitive items.

The fixture is large on purpose — a sampled run and a full run are observably
different at this size, and it forces the LLM tier to batch rather than loop.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

TOTAL = 220
BODY_MARKER = "ZZBODYMARKERZZ"  # must never reach the database

USER_ID = "user-fixture"
ACCOUNT_ID = "acct-fixture"

# Senders the user has genuinely corresponded with — never archivable.
REPLIED_SENDERS = ("maya@northwind.io", "dev@arcstack.dev", "sam@lighthouse.vc")
# A bulk sender the user has never opened and repeatedly archived.
BULK_SENDER = "promo@dealsdaily.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _thread(
    index: int,
    *,
    subject: str,
    from_name: str,
    from_email: str,
    snippet: str,
    list_id: str | None = None,
    unsubscribe_url: str | None = None,
    message_count: int = 1,
    has_attachments: bool = False,
    age_days: int = 3,
    is_unread: bool = True,
) -> dict:
    return {
        "id": f"item-{index:04d}",
        "external_thread_id": f"thread-{index:04d}",
        "external_message_ids": [f"msg-{index:04d}-{n}" for n in range(message_count)],
        "subject": subject,
        "from_name": from_name,
        "from_email": from_email,
        "from_domain": from_email.split("@")[-1],
        "to_emails": ["founder@example.com"],
        "cc_emails": [],
        "list_id": list_id,
        "unsubscribe_url": unsubscribe_url,
        "message_count": message_count,
        "has_attachments": has_attachments,
        # Deliberately oversized: the pipeline must truncate to <=200 chars, so the
        # marker parked past that boundary can never reach the database.
        "snippet": f"{snippet} " + "filler body text. " * 20 + BODY_MARKER,
        # A full body is attached the way an escalated fetch would attach one; it must
        # be dropped at the redaction chokepoint and never persisted.
        "body": f"Full message body. {BODY_MARKER} " + "paragraph after paragraph. " * 40,
        "internal_date": _now() - timedelta(days=age_days, hours=index % 23),
        "is_unread": is_unread,
        "channel_labels": ["INBOX", "UNREAD"] if is_unread else ["INBOX"],
    }


def build_threads() -> list[dict]:
    rng = random.Random(20260809)
    threads: list[dict] = []
    index = 0

    def add(**kwargs) -> None:
        nonlocal index
        threads.append(_thread(index, **kwargs))
        index += 1

    # --- 62 newsletters on Substack mailing lists (tier 1: List-Id rule) -----------
    publications = [
        ("The Diff", "thediff"),
        ("Lenny's Newsletter", "lennys"),
        ("Platformer", "platformer"),
        ("Stratechery Daily", "stratechery"),
        ("Import AI", "importai"),
    ]
    for n in range(62):
        title, slug = publications[n % len(publications)]
        add(
            subject=f"{title}: {rng.choice(['Issue', 'Vol.', 'No.'])} {200 + n}",
            from_name=title,
            from_email=f"{slug}@substack.com",
            snippet=f"This week: {rng.choice(['margins', 'AI agents', 'pricing', 'growth'])} "
            "and three links worth your time.",
            list_id=f"<{slug}.substack.com>",
            unsubscribe_url=f"https://{slug}.substack.com/unsubscribe",
            age_days=n % 30,
        )

    # --- 34 GitHub notifications (tier 1: domain rule) ----------------------------
    for n in range(34):
        repo = rng.choice(["acme/api", "acme/web", "acme/infra"])
        add(
            subject=f"[{repo}] {rng.choice(['CI failed on', 'New comment on', 'Merged'])} "
            f"PR #{1000 + n}",
            from_name="GitHub",
            from_email="notifications@github.com",
            snippet=f"{rng.choice(['build failed', 'review requested', 'branch merged'])} "
            f"in {repo}.",
            list_id=f"<{repo.replace('/', '.')}.github.com>",
            age_days=n % 14,
        )

    # --- 18 from a bulk sender never opened, repeatedly archived (tier 2) ----------
    for n in range(18):
        add(
            subject=f"{rng.choice(['48h only', 'Flash sale', 'Last chance'])} — "
            f"{rng.randint(20, 70)}% off everything",
            from_name="Deals Daily",
            from_email=BULK_SENDER,
            snippet="Our biggest discount of the season ends tonight. Shop now.",
            unsubscribe_url="https://dealsdaily.com/unsub",
            age_days=n % 21,
        )

    # --- 24 threads from people the user has replied to (tier 2: never-miss) ------
    for n in range(24):
        sender = REPLIED_SENDERS[n % len(REPLIED_SENDERS)]
        add(
            subject=rng.choice(
                ["Re: Q3 roadmap", "Re: contract redlines", "Re: intro to the team",
                 "Re: pricing experiment", "Re: next week"]
            ),
            from_name=sender.split("@")[0].title(),
            from_email=sender,
            snippet="Thanks for the notes — one more question before we lock this in.",
            message_count=rng.randint(2, 6),
            age_days=n % 10,
        )

    # --- 20 receipts and invoices -------------------------------------------------
    vendors = [
        ("Stripe", "receipts@stripe.com"),
        ("Amazon", "auto-confirm@amazon.com"),
        ("Apple", "no_reply@email.apple.com"),
        ("Vercel", "billing@vercel.com"),
    ]
    for n in range(20):
        vendor, email = vendors[n % len(vendors)]
        add(
            subject=f"Your {vendor} receipt #{5000 + n} — ${rng.randint(9, 480)}.00",
            from_name=vendor,
            from_email=email,
            snippet=f"Thanks for your payment. Card ending 4242 charged "
            f"${rng.randint(9, 480)}.00 on your {vendor} account.",
            has_attachments=n % 3 == 0,
            age_days=n % 25,
        )

    # --- 20 recruiter outreach ----------------------------------------------------
    for n in range(20):
        add(
            subject=rng.choice(
                ["Staff Engineer role at a Series B", "Are you open to new roles?",
                 "Quick question about your background"]
            ),
            from_name=f"Recruiter {n}",
            from_email=f"talent{n}@{rng.choice(['hireloop.io', 'talentcache.com'])}",
            snippet="I came across your profile and thought you'd be a great fit for a "
            "role I'm working on.",
            age_days=n % 18,
        )

    # --- 12 cold sales outreach ---------------------------------------------------
    for n in range(12):
        add(
            subject=rng.choice(
                ["Cutting your infra bill by 40%", "15 minutes next week?",
                 "Following up on my last email"]
            ),
            from_name=f"SDR {n}",
            from_email=f"sales{n}@{rng.choice(['growthpilot.co', 'pipelabs.ai'])}",
            snippet="Just bumping this to the top of your inbox — worth a quick chat?",
            age_days=n % 12,
        )

    # --- 12 genuine person-to-person mail from new senders ------------------------
    for n in range(12):
        add(
            subject=rng.choice(
                ["Intro: you two should talk", "Question about your API",
                 "Following up from the conference", "Coffee next week?"]
            ),
            from_name=f"Person {n}",
            from_email=f"person{n}@{rng.choice(['gmail.com', 'hey.com', 'fastmail.com'])}",
            snippet="We met briefly last week — I'd love to pick your brain about how "
            "you handle onboarding.",
            message_count=rng.randint(1, 3),
            age_days=n % 9,
        )

    # --- 10 time-sensitive / high-stakes -----------------------------------------
    urgent = [
        ("Action required: invoice #4471 is 14 days overdue", "billing@vendorworks.com"),
        ("Security alert: new sign-in from an unrecognised device", "security@accounts.io"),
        ("Your domain example.com expires in 5 days", "renewals@registrar.net"),
        ("Final notice: tax filing deadline is Friday", "notices@statetax.gov"),
        ("Your card on file was declined — service pauses in 48h", "billing@cloudhost.com"),
    ]
    for n in range(10):
        subject, email = urgent[n % len(urgent)]
        add(
            subject=subject,
            from_name=email.split("@")[-1].split(".")[0].title(),
            from_email=email,
            snippet="Immediate action is required to avoid interruption. "
            "Your verification code is 483920 and your key is sk-abcdefghijklmnopqrstuv.",
            age_days=n % 4,
        )

    # --- 8 ambiguous odds and ends ------------------------------------------------
    for n in range(8):
        add(
            subject=rng.choice(["(no subject)", "FYI", "update", "?"]),
            from_name="",
            from_email=f"unknown{n}@mailer{n}.net",
            snippet="see attached",
            has_attachments=n % 2 == 0,
            age_days=n % 30,
        )

    assert len(threads) == TOTAL, f"fixture must hold {TOTAL} threads, got {len(threads)}"
    return threads


def seed_user(session) -> None:
    """Seed the user, mailbox, taxonomy, active rules and sender history."""
    from db.models import (
        Category,
        ChannelAccount,
        Rule,
        SenderProfile,
        User,
        UserSettings,
    )
    from tools.rules import DEFAULT_TAXONOMY

    session.add(User(id=USER_ID, email="founder@example.com", display_name="Founder"))
    session.add(
        ChannelAccount(
            id=ACCOUNT_ID,
            user_id=USER_ID,
            channel="gmail",
            account_email="founder@example.com",
            refresh_token_enc="encrypted",
            scopes=["gmail.readonly"],
            status="connected",
        )
    )
    session.add(UserSettings(user_id=USER_ID, confidence_floor=0.75, dry_run=True))

    for order, category in enumerate(DEFAULT_TAXONOMY):
        session.add(
            Category(
                id=f"cat-{category['key']}",
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

    session.add(
        Rule(
            id="rule-substack",
            user_id=USER_ID,
            name="Substack newsletters",
            kind="deterministic",
            source="seed_pack",
            matcher={"list_id": "substack.com"},
            action={"set_category": "newsletters", "archive": True},
            status="active",
            confidence=0.96,
        )
    )
    session.add(
        Rule(
            id="rule-github",
            user_id=USER_ID,
            name="GitHub notifications",
            kind="deterministic",
            source="seed_pack",
            matcher={"from_domain": "github.com"},
            action={"set_category": "notifications", "archive": True},
            status="active",
            confidence=0.93,
        )
    )
    session.add(
        Rule(
            id="rule-disabled",
            user_id=USER_ID,
            name="Disabled catch-all (must never fire)",
            kind="deterministic",
            source="user",
            matcher={"subject_regex": ".*"},
            action={"set_category": "notifications", "archive": True},
            status="disabled",
            confidence=0.5,
        )
    )

    for sender in REPLIED_SENDERS:
        session.add(
            SenderProfile(
                user_id=USER_ID,
                sender_email=sender,
                sender_domain=sender.split("@")[-1],
                received_count=31,
                opened_count=28,
                replied_count=12,
                archived_by_user_count=0,
                ever_replied=True,
                last_replied_at=_now() - timedelta(days=2),
                last_seen_at=_now(),
                importance_score=0.95,
            )
        )
    session.add(
        SenderProfile(
            user_id=USER_ID,
            sender_email=BULK_SENDER,
            sender_domain="dealsdaily.com",
            received_count=64,
            opened_count=0,
            replied_count=0,
            archived_by_user_count=51,
            ever_replied=False,
            last_seen_at=_now(),
            importance_score=0.05,
        )
    )
