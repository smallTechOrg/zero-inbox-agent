"""The never-miss reviewer must catch what matters — and only what matters.

Measured on a real 300-thread run: the reviewer flipped 55 of ~119 archive
proposals back to keep (a 46% veto rate), including Facebook "you have 11
notifications" digests, Facebook group/page suggestions and PayPal marketing.
Its own justifications gave it away — "could cause missed important
communications" (speculation, which the prompt forbids) and, worse, "Facebook
group suggestion email with promotional content and List-Id", which is an
argument for ARCHIVING attached to a flip-to-keep.

That made the reviewer the binding constraint on inbox zero: a thread it vetoes
stays in the inbox forever. But the fix must not buy throughput with safety, so
this file pins BOTH directions against the real model.

Real NVIDIA calls; skipped only if no key is configured.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


#: Wrongly proposed for archive. Missing any of these is the failure mode the
#: whole product exists to prevent.
MUST_KEEP = [
    ("b1", "Your invoice #4821 is overdue — payment due Aug 20", "billing@stripe.com",
     "Your payment of $249.00 failed. Update your card by August 20 to avoid service interruption.",
     "newsletters"),
    ("b2", "Security alert: new sign-in from Kyiv, Ukraine", "no-reply@accounts.google.com",
     "A device you don't usually use just signed in to your account.", "notifications"),
    ("b3", "Re: our contract — signature needed by Friday", "priya@acmelegal.com",
     "Attaching the revised MSA. We need your signature before Friday or the renewal lapses.",
     "newsletters"),
    ("b4", "Action required: your domain expires in 3 days", "renewals@namecheap.com",
     "madhyamakist.com expires on Aug 18. Renew to avoid losing the domain.", "notifications"),
    ("b5", "Court date scheduled — case 2026/CV/882", "clerk@courts.gov.in",
     "Your hearing is scheduled for 12 September. Attendance is mandatory.", "newsletters"),
    ("b6", "Your package could not be delivered — action needed", "noreply@bluedart.com",
     "We attempted delivery. Reschedule within 48h or the parcel returns to sender.",
     "notifications"),
]

#: Real subjects the reviewer actually vetoed on run 92c7d3fe. Every one of these
#: must archive — they are exactly what stood between the user and inbox zero.
MUST_ARCHIVE = [
    ("c1", "You have 11 notifications about Psy and others", "notification@facebookmail.com",
     "See what you missed on Facebook.", "notifications"),
    ("c2", "Sangeeta and 7 others are new Group suggestions for you", "notification@facebookmail.com",
     "Discover groups you might like.", "newsletters"),
    ("c3", "You have 8 new Page suggestions including Glory Goa.", "notification@facebookmail.com",
     "Pages you may want to follow.", "newsletters"),
    ("c4", "Looking for ways to maximise growth for your online business",
     "marketing@paypal.com", "Tips and tools to grow your business with PayPal.", "receipts"),
    ("c5", "Flat 40% off — this weekend only!", "offers@myntra.com",
     "Shop the biggest sale of the season.", "newsletters"),
]


def _payloads(rows):
    decisions = [
        {"item_id": r[0], "proposed_action": "archive", "category": r[4],
         "confidence": 0.85, "reasoning": "first-pass: bulk/promotional"}
        for r in rows
    ]
    items = {
        r[0]: {"id": r[0], "subject": r[1], "from_email": r[2], "from_name": "",
               "from_domain": r[2].split("@")[-1], "snippet_redacted": r[3], "list_id": None,
               "message_count": 1, "has_attachments": False, "is_unread": True}
        for r in rows
    }
    return decisions, items


@pytest.fixture(scope="module")
def verdicts():
    from config.settings import get_settings
    from graph.nodes_review import review_archive_batch

    if not get_settings().nvidia_api_key:
        pytest.skip("AGENT_NVIDIA_API_KEY not configured")

    rows = MUST_KEEP + MUST_ARCHIVE
    decisions, items = _payloads(rows)
    flips, _usage, failed = review_archive_batch(decisions, items, priorities="")
    assert not failed, "the reviewer batch failed outright — cannot judge calibration"
    return flips


@pytest.mark.parametrize("row", MUST_KEEP, ids=[r[0] for r in MUST_KEEP])
def test_the_reviewer_catches_genuinely_important_mail(verdicts, row):
    """A missed flip here is mail the user loses. This direction may never regress."""
    assert row[0] in verdicts, (
        f"reviewer failed to flip {row[1]!r} back to keep — this is the never-miss "
        "guarantee failing, and it is the one thing this system must not get wrong"
    )


@pytest.mark.parametrize("row", MUST_ARCHIVE, ids=[r[0] for r in MUST_ARCHIVE])
def test_the_reviewer_does_not_veto_ordinary_bulk_mail(verdicts, row):
    """Each of these was a REAL false veto on run 92c7d3fe. A thread vetoed here
    stays in the inbox forever, so over-caution is not a safe default — it is the
    thing that makes inbox zero unreachable."""
    assert row[0] not in verdicts, (
        f"reviewer vetoed {row[1]!r}; speculative caution on bulk mail is what "
        "kept 55 of 119 archive proposals in the inbox"
    )
