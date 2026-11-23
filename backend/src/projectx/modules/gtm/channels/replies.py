"""Replying to comments and messages on each platform."""

from __future__ import annotations

import hashlib
from typing import Any

from projectx.modules.gtm.channels.adapters.base import AdapterContext, PlatformRejected, raise_for_status
from projectx.modules.gtm.channels.adapters.meta import GRAPH_VERSION
from projectx.modules.gtm.models import ChannelAccount


async def reply(
    services: Any,
    account: ChannelAccount,
    ctx: AdapterContext,
    *,
    platform: str,
    kind: str,
    target_id: str,
    thread_id: str | None,
    text: str,
) -> dict[str, Any]:
    http = ctx.http
    if account.status == "practice":
        return {"external_id": "sim_reply_" + hashlib.sha256(f"{target_id}:{text}".encode()).hexdigest()[:12]}
    graph = f"https://graph.facebook.com/{ctx.settings.get('graph_version', GRAPH_VERSION)}"
    if platform == "instagram" and kind == "comment":
        response = await http.post(
            f"{graph}/{target_id}/replies", data={"message": text, "access_token": ctx.credentials["access_token"]}
        )
        raise_for_status(response, "Instagram")
        return {"external_id": str(response.json().get("id"))}
    if platform == "facebook" and kind == "comment":
        response = await http.post(
            f"{graph}/{target_id}/comments",
            data={"message": text, "access_token": ctx.credentials["page_access_token"]},
        )
        raise_for_status(response, "Facebook")
        return {"external_id": str(response.json().get("id"))}
    if platform in ("facebook", "instagram") and kind == "dm":
        token = ctx.credentials.get("page_access_token") or ctx.credentials["access_token"]
        page = ctx.settings.get("page_id") or account.external_id
        response = await http.post(
            f"{graph}/{page}/messages",
            json={
                "recipient": {"id": thread_id or target_id},
                "message": {"text": text},
                "messaging_type": "RESPONSE",
                "access_token": token,
            },
        )
        raise_for_status(response, "Messenger")
        return {"external_id": str(response.json().get("message_id"))}
    if platform == "x":
        response = await http.post(
            "https://api.x.com/2/tweets",
            json={"text": text, "reply": {"in_reply_to_tweet_id": target_id}},
            headers={"Authorization": f"Bearer {ctx.credentials['access_token']}"},
        )
        raise_for_status(response, "X")
        return {"external_id": str(response.json()["data"]["id"])}
    if platform == "threads":
        api = "https://graph.threads.net/v1.0"
        token = ctx.credentials["access_token"]
        created = await http.post(
            f"{api}/{account.external_id}/threads",
            data={"media_type": "TEXT", "text": text, "reply_to_id": target_id, "access_token": token},
        )
        raise_for_status(created, "Threads")
        published = await http.post(
            f"{api}/{account.external_id}/threads_publish",
            data={"creation_id": created.json()["id"], "access_token": token},
        )
        raise_for_status(published, "Threads")
        return {"external_id": str(published.json()["id"])}
    if platform == "telegram":
        response = await http.post(
            f"https://api.telegram.org/bot{ctx.credentials['bot_token']}/sendMessage",
            json={
                "chat_id": thread_id or ctx.settings.get("chat_id"),
                "text": text,
                "reply_parameters": {"message_id": int(target_id.rsplit(":", maxsplit=1)[-1])}
                if target_id.rsplit(":", maxsplit=1)[-1].isdigit()
                else None,
            },
        )
        raise_for_status(response, "Telegram")
        return {"external_id": str(response.json().get("result", {}).get("message_id"))}
    if platform == "whatsapp":
        from projectx.modules.gtm.channels.whatsapp import WhatsAppAdapter

        # Replying to someone who just wrote to us is always inside the 24-hour window.
        ctx.settings.setdefault("phone_number_id", account.external_id)
        message_id = await WhatsAppAdapter().send(ctx, to=str(thread_id or target_id), text=text)
        return {"external_id": message_id}
    raise PlatformRejected(f"Replying on {platform} isn't supported yet; reply in the app")
