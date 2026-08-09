"""Channel adapters. The triage core talks only to `ChannelAdapter`."""

from channels.base import (
    ChannelAdapter,
    ChannelError,
    ChannelItem,
    DryRunViolation,
    RateLimited,
    ReauthRequired,
    SenderSignal,
)

def get_adapter(*, user_id: str, channel_account_id: str, **kwargs) -> ChannelAdapter:
    """Resolve the adapter for one connected account.

    The single entry point the triage graph uses to reach a channel; it must not
    know that Gmail is the only implementation today. Imported lazily so the core
    does not pull googleapiclient in at module import.
    """
    from channels.gmail.store import adapter_for_connection

    return adapter_for_connection(
        user_id=user_id, connection_id=channel_account_id, **kwargs
    )


__all__ = [
    "get_adapter",
    "ChannelAdapter",
    "ChannelError",
    "ChannelItem",
    "DryRunViolation",
    "RateLimited",
    "ReauthRequired",
    "SenderSignal",
]
