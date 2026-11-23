"""Choosing the adapter for an account: practice → simulator, unified API → Ayrshare, else the platform."""

from __future__ import annotations

from projectx.kernel.errors import NotConfigured
from projectx.modules.gtm.channels.adapters.base import Adapter
from projectx.modules.gtm.channels.adapters.messaging import DiscordAdapter, TelegramAdapter
from projectx.modules.gtm.channels.adapters.meta import FacebookPageAdapter, InstagramAdapter, ThreadsAdapter
from projectx.modules.gtm.channels.adapters.others import (
    AyrshareAdapter,
    LinkedInAdapter,
    PinterestAdapter,
    TikTokAdapter,
    YouTubeAdapter,
)
from projectx.modules.gtm.channels.adapters.simulator import SimulatorAdapter
from projectx.modules.gtm.channels.adapters.x import XAdapter
from projectx.modules.gtm.models import ChannelAccount
from projectx.settings import Settings

_NATIVE: dict[str, type] = {
    "x": XAdapter,
    "telegram": TelegramAdapter,
    "discord": DiscordAdapter,
    "facebook": FacebookPageAdapter,
    "instagram": InstagramAdapter,
    "threads": ThreadsAdapter,
    "linkedin": LinkedInAdapter,
    "pinterest": PinterestAdapter,
    "tiktok": TikTokAdapter,
    "youtube": YouTubeAdapter,
}


def adapter_for(account: ChannelAccount, settings: Settings) -> Adapter:
    provider = (account.settings or {}).get("provider")
    if account.status == "practice" or provider == "simulator":
        return SimulatorAdapter(account.platform)
    if provider == "ayrshare":
        if not settings.ayrshare_api_key:
            raise NotConfigured("Unified API (Ayrshare) is not configured")
        return AyrshareAdapter(account.platform, settings.ayrshare_api_key.get_secret_value())
    if account.platform == "whatsapp":  # imported lazily: the WhatsApp module builds on these adapters
        from projectx.modules.gtm.channels.whatsapp import WhatsAppAdapter

        return WhatsAppAdapter()
    cls = _NATIVE.get(account.platform)
    if cls is None:
        raise NotConfigured(f"No adapter for {account.platform}")
    adapter: Adapter = cls()
    return adapter
