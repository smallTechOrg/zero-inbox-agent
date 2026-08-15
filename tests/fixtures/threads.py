"""Synthetic INBOX thread metadata for triage tests.

Every thread carries ``BODY_SENTINEL`` in its (never-allowed) body field so any
test can prove the privacy boundary: if the sentinel appears in an outgoing LLM
payload, a body leaked (spec/architecture.md § Privacy Boundary).
"""

from __future__ import annotations

BODY_SENTINEL = "PRIVACY-SENTINEL-b0dy-must-never-reach-an-llm-7f3a"

#: Privacy-allowed classifier fields, verbatim from spec/architecture.md.
ALLOWED_CLASSIFIER_FIELDS = {
    "thread_id",
    "gmail_thread_id",
    "list_unsubscribe_present",
    "thread_message_count",
    "sender",              # address + display name
    "sender_name",
    "sender_address",
    "subject",
    "has_list_unsubscribe",
    "list_unsubscribe",
    "reply_to",
    "category_tab",
    "message_count",
    "thread_size",
    "has_user_replied",
    "snippet",
}

#: (thread_id, sender, subject, snippet, category_tab, expected-ish category)
#: Deliberately obvious senders so a real cheap model files them consistently.
SAMPLE_THREADS = [
    {
        "thread_id": "test-thr-001",
        "sender": "ACME Billing <billing@acme-invoices.example.com>",
        "subject": "Your ACME invoice #4821 is due",
        "snippet": "Invoice #4821 for $120.00 is due on Sep 1. View your bill…",
        "category_tab": "personal",
        "has_list_unsubscribe": False,
        "reply_to": "billing@acme-invoices.example.com",
        "message_count": 1,
        "has_user_replied": False,
        "body": BODY_SENTINEL,
    },
    {
        "thread_id": "test-thr-002",
        "sender": "Morning Brew <crew@morningbrew.example.com>",
        "subject": "☕ Today's newsletter: markets, tech, and more",
        "snippet": "Good morning. Here's everything you need to know today…",
        "category_tab": "promotions",
        "has_list_unsubscribe": True,
        "reply_to": "crew@morningbrew.example.com",
        "message_count": 1,
        "has_user_replied": False,
        "body": BODY_SENTINEL,
    },
    {
        "thread_id": "test-thr-003",
        "sender": "GitHub <notifications@github.example.com>",
        "subject": "[repo] PR #42 was merged",
        "snippet": "Merged #42 into main. View it on GitHub…",
        "category_tab": "updates",
        "has_list_unsubscribe": True,
        "reply_to": "noreply@github.example.com",
        "message_count": 3,
        "has_user_replied": False,
        "body": BODY_SENTINEL,
    },
    {
        "thread_id": "test-thr-004",
        "sender": "Mum <mum@family.example.com>",
        "subject": "Sunday lunch?",
        "snippet": "Are you coming over on Sunday? Dad is making his famous…",
        "category_tab": "personal",
        "has_list_unsubscribe": False,
        "reply_to": "mum@family.example.com",
        "message_count": 5,
        "has_user_replied": True,
        "body": BODY_SENTINEL,
    },
]


def sample_threads(n: int | None = None) -> list[dict]:
    """A fresh copy of the first ``n`` sample threads (all, if ``n`` is None)."""
    picked = SAMPLE_THREADS if n is None else SAMPLE_THREADS[:n]
    return [dict(t) for t in picked]
