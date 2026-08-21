"""The inbox mini-audit data source (spec/capabilities/inbox-audit.md).

Strictly read-only: this module issues ONLY ``threads().list`` and
``threads().get`` requests — it never constructs a :class:`GmailMutator` and has
no code path to any Gmail write endpoint.

Fast by construction (<10s on a 5,000-thread inbox):

* totals use Gmail's ``resultSizeEstimate`` (labelled approximate);
* top senders come from a newest-first *sample* of thread metadata;
* the oldest-thread age is found by a bounded binary search over
  ``in:inbox older_than:<n>d`` estimates (~11 cheap list calls).

Everything is scoped ``in:inbox`` — archived mail is never counted or fetched.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from channels.gmail.adapter import GmailAdapter

#: Gmail category tabs, as search operators, in display order.
CATEGORY_TABS: tuple[str, ...] = ("primary", "social", "promotions", "updates", "forums")

#: How many newest threads to sample for the top-senders slice.
DEFAULT_SAMPLE_SIZE = 100

#: Upper bound of the oldest-thread binary search (~10 years).
MAX_OLDEST_DAYS = 3650

TOP_SENDERS = 10


def _estimate(adapter: GmailAdapter, query: str) -> int:
    """Gmail's estimated thread count for ``query`` — one cheap list call."""
    page = adapter._execute(
        adapter._service.users()
        .threads()
        .list(userId="me", q=query, maxResults=1)
    )
    return int((page or {}).get("resultSizeEstimate") or 0)


def _oldest_days(adapter: GmailAdapter) -> int:
    """Approximate age in days of the oldest INBOX thread, via binary search.

    Invariant: ``in:inbox older_than:<n>d`` is non-empty iff the oldest thread is
    older than ``n`` days. Search the largest such ``n``.
    """
    if _estimate(adapter, "in:inbox older_than:1d") == 0:
        return 0
    low, high = 1, MAX_OLDEST_DAYS  # low: known non-empty, high: assumed empty
    if _estimate(adapter, f"in:inbox older_than:{MAX_OLDEST_DAYS}d") > 0:
        return MAX_OLDEST_DAYS
    while high - low > 1:
        mid = (low + high) // 2
        if _estimate(adapter, f"in:inbox older_than:{mid}d") > 0:
            low = mid
        else:
            high = mid
    return low


def collect_inbox_snapshot(
    adapter: GmailAdapter, *, sample_size: int = DEFAULT_SAMPLE_SIZE
) -> dict:
    """Return the mini-audit payload for one user's INBOX. Zero mutations.

    Shape matches the ``inbox_snapshots`` entity in spec/data.md::

        {
          "total_inbox_threads": int,   # Gmail estimate
          "unread": int,                # Gmail estimate
          "oldest_days": int,           # binary-search approximation
          "top_senders": [{"address": str, "count": int}, ...],  # sampled
          "category_tab_counts": {tab: int, ...},                # estimates
          "approximate": True,
          "sample_size": int,
          "collected_at": iso8601,
        }
    """
    total = _estimate(adapter, "in:inbox")
    unread = _estimate(adapter, "in:inbox is:unread")
    tab_counts = {
        tab: _estimate(adapter, f"in:inbox category:{tab}") for tab in CATEGORY_TABS
    }

    # Top senders from the newest slice — metadata only, headers + snippet.
    items = adapter.list_threads(limit=sample_size)
    counts = Counter(item.from_email for item in items if item.from_email)
    top_senders = [
        {"address": address, "count": count}
        for address, count in counts.most_common(TOP_SENDERS)
    ]

    return {
        "total_inbox_threads": total,
        "unread": unread,
        "oldest_days": _oldest_days(adapter),
        "top_senders": top_senders,
        "category_tab_counts": tab_counts,
        "approximate": True,
        "sample_size": len(items),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def _view_field(view, *names: str):
    """First present field of a listing view (mapping or ClassifierView)."""
    for name in names:
        if isinstance(view, dict):
            if view.get(name) not in (None, ""):
                return view[name]
        elif getattr(view, name, None) not in (None, ""):
            return getattr(view, name)
    return None


def snapshot_from_views(views) -> dict:
    """Degraded snapshot derived purely from the thread-listing seam.

    Used when the estimate calls are unavailable after a successful listing
    (and by the seam tests, which fake the listing). Same shape as
    :func:`collect_inbox_snapshot`; totals are capped by the listing limit.
    """
    senders = Counter()
    tab_counts: Counter = Counter()
    for view in views:
        address = _view_field(view, "sender_address", "sender", "from_email")
        if address:
            senders[str(address)] += 1
        category = _view_field(view, "gmail_category")
        if category:
            tab_counts[str(category)] += 1
    return {
        "total_inbox_threads": len(views),
        "unread": 0,
        "oldest_days": 0,
        "top_senders": [
            {"address": address, "count": count}
            for address, count in senders.most_common(TOP_SENDERS)
        ],
        "category_tab_counts": dict(tab_counts),
        "approximate": True,
        "sample_size": len(views),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
